"""还原器单元测试：识别并坍缩历史版本（v1.3~v1.8）留下的守卫包装。

v1.9 起本插件不再运行时修改任何 handler，只在启动时做一次性还原。
运行方式（插件根目录下）：python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import asyncio
import functools
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hook_guard import (  # noqa: E402
    HookGuard,
    _is_guard_wrapper,
    _unwrap_guard,
)


def run(coro):
    return asyncio.run(coro)


class VictimPlugin:
    """被历史版本包装过的第三方插件（模拟 on_llm_request(self, event, req)）。"""

    def __init__(self) -> None:
        self.received = None

    async def on_llm_request(self, event, req) -> None:
        self.received = (event, req)


def make_old_guard(original, layers=1, inflate=True):
    """复刻历史版本守卫：带 _gc_guarded/_gc_orig 标记，可选每层多塞一个 event。"""
    fn = original
    for _ in range(layers):
        if inflate:
            body = lambda event, *args, _fn=fn: _fn(event, event, *args)
        else:
            body = lambda event, *args, _fn=fn: _fn(event, *args)

        async def guarded(*args, _body=body, **kwargs):
            return await _body(*args, **kwargs)

        guarded.__module__ = "astrbot_plugin_group_config.core.hook_guard"
        guarded._gc_guarded = True  # 历史版本同样打标记
        guarded._gc_orig = fn
        fn = guarded
    return fn


class FakeEventType:
    def __init__(self, name: str = "OnLLMRequestEvent") -> None:
        self.name = name


class FakeHandlerMd:
    def __init__(self, full_name, handler_name, module_path, handler):
        self.handler_full_name = full_name
        self.handler_name = handler_name
        self.handler_module_path = module_path
        self.handler = handler
        self.event_type = FakeEventType()


class FakeTool:
    def __init__(self, name, handler, module_path):
        self.name = name
        self.handler = handler
        self.handler_module_path = module_path


class TestUnwrap(unittest.TestCase):
    def test_detect_by_flag_and_module(self):
        victim = VictimPlugin()
        old = make_old_guard(victim.on_llm_request, layers=1)
        self.assertTrue(_is_guard_wrapper(old))
        self.assertFalse(_is_guard_wrapper(victim.on_llm_request))

    def test_no_false_positive_on_plugin_own_wraps(self):
        async def business(x):
            return x

        @functools.wraps(business)
        async def wrapper(x):
            return await business(x)

        self.assertFalse(_is_guard_wrapper(wrapper))
        self.assertIs(_unwrap_guard(wrapper), wrapper)

    def test_collapse_multi_layer_chain(self):
        victim = VictimPlugin()
        true_original = victim.on_llm_request
        chain = make_old_guard(true_original, layers=4)
        self.assertTrue(_is_guard_wrapper(chain))
        self.assertIs(_unwrap_guard(chain), true_original)

    def test_detect_and_unwrap_partial_wrapped_guard(self):
        # 核心 star_manager.load() 会把注册表里"当前的 handler"再 partial 一个实例；
        # functools.partial 不转发属性，必须穿透这层才能认出残留的包装器。
        victim = VictimPlugin()
        true_original = victim.on_llm_request
        bound = functools.partial(make_old_guard(true_original, layers=2), victim)
        self.assertTrue(_is_guard_wrapper(bound))
        self.assertIs(_unwrap_guard(bound), true_original)

    def test_unwrap_partial_bound_gate_rebinds_to_latest_instance(self):
        # v3.0 闸门：记录的原始值本身是 partial(raw, 旧实例) → 还原成 partial(raw, 新实例)
        victim_a, victim_b = VictimPlugin(), VictimPlugin()
        raw = VictimPlugin.on_llm_request

        async def gate(instance, *args, **kwargs):
            return await raw(instance, *args, **kwargs)

        gate._gc_guarded = True
        gate._gc_raw = raw
        gate._gc_orig = functools.partial(raw, victim_a)
        out = _unwrap_guard(functools.partial(gate, victim_b))
        self.assertIsInstance(out, functools.partial)
        self.assertIs(out.func, raw)
        self.assertIs(out.args[0], victim_b)


class TestCleanup(unittest.TestCase):
    """在 stub 掉的注册表/工具表上验证一次性还原。"""

    def setUp(self):
        self.victim = VictimPlugin()
        self.true_original = self.victim.on_llm_request
        self.md = FakeHandlerMd(
            "victim.main_on_llm_request",
            "on_llm_request",
            "victim.main",
            make_old_guard(self.true_original, layers=3),
        )
        self.tool = FakeTool(
            "victim_tool",
            make_old_guard(self.true_original, layers=2),
            "victim.main",
        )
        self._saved = {}
        for name in (
            "astrbot",
            "astrbot.core",
            "astrbot.core.star",
            "astrbot.core.star.star_handler",
            "astrbot.core.provider",
            "astrbot.core.provider.register",
        ):
            self._saved[name] = sys.modules.get(name)
            sys.modules[name] = types.ModuleType(name)
        sys.modules["astrbot.core.star.star_handler"].star_handlers_registry = [self.md]
        sys.modules["astrbot.core.provider.register"].llm_tools = types.SimpleNamespace(
            func_list=[self.tool]
        )
        self.guard = HookGuard()

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_restores_handler_to_original(self):
        self.assertTrue(_is_guard_wrapper(self.md.handler))  # 前置：确实是被包装的
        restored = run(self.guard.cleanup())
        self.assertEqual(restored, 2)  # handler + 工具各一条
        self.assertIs(self.md.handler, self.true_original)  # 换回原始函数
        self.assertIs(self.tool.handler, self.true_original)

    def test_restored_handler_works_without_extra_args(self):
        # 历史版本多塞 event 会让它炸（takes N but M were given）；还原后应当正常
        run(self.guard.cleanup())
        event, req = object(), object()
        run(self.md.handler(event, req))
        self.assertEqual(self.victim.received, (event, req))

    def test_idempotent(self):
        run(self.guard.cleanup())
        self.assertEqual(run(self.guard.cleanup()), 0)  # 第二次无事可做
        self.assertIs(self.md.handler, self.true_original)

    def test_ignores_plain_handlers(self):
        raw_md = FakeHandlerMd(
            "victim.main_on_message", "on_message", "victim.main", self.true_original
        )
        sys.modules["astrbot.core.star.star_handler"].star_handlers_registry = [raw_md]
        sys.modules["astrbot.core.provider.register"].llm_tools = types.SimpleNamespace(
            func_list=[]
        )
        self.assertEqual(run(self.guard.cleanup()), 0)
        self.assertIs(raw_md.handler, self.true_original)  # 原样不动

    def test_cleanup_repairs_partial_wrapped_handler(self):
        # 现场残骸形态：核心把我们的包装器 partial 掉了一个新实例
        self.md.handler = functools.partial(
            make_old_guard(self.true_original, layers=2), self.victim
        )
        sys.modules["astrbot.core.provider.register"].llm_tools = types.SimpleNamespace(
            func_list=[]
        )
        self.assertEqual(run(self.guard.cleanup()), 1)
        event, req = object(), object()
        run(self.md.handler(event, req))  # 还原后可直接调用，不会 takes N but M
        self.assertEqual(self.victim.received, (event, req))

    def test_keeps_guard_mark_but_unwrappable_marker(self):
        # 认得出标记但解不到底（没有 original 可追）：不动，避免误伤
        async def fake_guarded(*args, **kwargs):
            return None

        fake_guarded.__module__ = "astrbot_plugin_group_config.core.hook_guard"
        fake_guarded._gc_guarded = True  # 有标记但没有 _gc_orig/闭包
        self.md.handler = fake_guarded
        sys.modules["astrbot.core.provider.register"].llm_tools = types.SimpleNamespace(
            func_list=[]
        )
        self.assertEqual(run(self.guard.cleanup()), 0)
        self.assertIs(self.md.handler, fake_guarded)


if __name__ == "__main__":
    unittest.main()
