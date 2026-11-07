"""会话级插件闸门（v3.0 与核心绑定契约同形版）单元测试。

运行方式（插件根目录下）：python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.session_gate as sg  # noqa: E402
from core.session_gate import (  # noqa: E402
    SessionGate,
    invalidate_session_cache,
    session_disabled_plugins,
)


def run(coro):
    return asyncio.run(coro)


class FakeEventType:
    def __init__(self, name: str = "OnLLMRequestEvent") -> None:
        self.name = name


class FakeHandlerMd:
    def __init__(self, full_name, handler_name, module_path, handler, enabled=True):
        self.handler_full_name = full_name
        self.handler_name = handler_name
        self.handler_module_path = module_path
        self.handler = handler
        self.event_type = FakeEventType()
        self.enabled = enabled


class FakePlugin:
    def __init__(self, name, reserved=False):
        self.name = name
        self.reserved = reserved


class FakeTool:
    def __init__(self, name, handler, module_path):
        self.name = name
        self.handler = handler
        self.handler_module_path = module_path


def rebind_like_core(holder, instance):
    """逐字复刻 star_manager.load() 的绑定循环（star_manager.py:1265-1276）。

    核心会在每次 load() 时把注册表里"当前的 handler"当成未绑定函数，再 partial
    一个插件实例；v2.2 的普通函数包装器正是在这里被多塞了一个实例。
    """
    raw_handler = (
        holder.handler.func
        if isinstance(holder.handler, functools.partial)
        else holder.handler
    )
    holder.handler = raw_handler
    holder.handler = functools.partial(raw_handler, instance)


class FakeSp:
    """astrbot.core.sp 替身：session_plugin_config 由 disabled 集合驱动。"""

    disabled: set = set()

    @staticmethod
    async def get_async(**kwargs):
        umo = kwargs.get("scope_id")
        return {umo: {"disabled_plugins": sorted(FakeSp.disabled)}}


class FakeEvent:
    def __init__(self, umo="default:GroupMessage:1_2"):
        self.unified_msg_origin = umo


class FakeContext:
    def __init__(self, event):
        self.context = types.SimpleNamespace(event=event)


class VictimPlugin:
    """被接管的第三方插件。"""

    def __init__(self) -> None:
        self.received = None

    async def on_llm_request(self, event, req) -> None:
        self.received = (event, req)


def make_asyncgen_handler(sink: list):
    """洋葱模型（异步生成器）形态的 handler。"""

    async def handler(event, *extra):
        sink.append(("start", len(extra)))
        yield "chunk-1"
        yield "chunk-2"

    return handler


class BaseCase(unittest.TestCase):
    MODULES = (
        "astrbot",
        "astrbot.core",
        "astrbot.core.star",
        "astrbot.core.star.star",
        "astrbot.core.star.star_handler",
        "astrbot.core.provider",
        "astrbot.core.provider.register",
        "astrbot.core.platform",
        "astrbot.core.platform.astr_message_event",
    )

    def setUp(self):
        self._saved = {}
        for name in self.MODULES:
            self._saved[name] = sys.modules.get(name)
            sys.modules[name] = types.ModuleType(name)
        sys.modules["astrbot.core"].sp = FakeSp
        sys.modules["astrbot.core.platform.astr_message_event"].AstrMessageEvent = (
            FakeEvent
        )
        self.star_map = {
            "victim.main": FakePlugin("victim_plugin"),
            "other.main": FakePlugin("other_plugin"),
            "reserved.main": FakePlugin("reserved_plugin", reserved=True),
            "self.main": FakePlugin("astrbot_plugin_group_config"),
        }
        sys.modules["astrbot.core.star.star"].star_map = self.star_map
        self.registry = self._Registry()
        sys.modules["astrbot.core.star.star_handler"].star_handlers_registry = (
            self.registry
        )
        sys.modules["astrbot.core.provider.register"].llm_tools = types.SimpleNamespace(
            func_list=[]
        )
        FakeSp.disabled = set()
        invalidate_session_cache()
        self.victim = VictimPlugin()
        self.true_original = self.victim.on_llm_request
        self.gate = SessionGate()

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        invalidate_session_cache()

    class _Registry(list):
        """带 star_handlers_map 的注册表替身（核心真身是 StarHandlerRegistry）。"""

        def __init__(self):
            super().__init__()
            self.star_handlers_map = {}

        def refresh_map(self):
            self.star_handlers_map = {h.handler_full_name: h for h in self}

    def make_md(self, full_name="victim.main_on_llm_request", handler=None,
                module_path="victim.main"):
        md = FakeHandlerMd(
            full_name, "on_llm_request", module_path,
            handler if handler is not None else self.true_original,
        )
        return md


class TestCallTimeFilter(BaseCase):
    """核心承诺：被禁用插件的调用在发生那一刻被跳过，参数一个不动。"""

    def setUp(self):
        super().setUp()
        self.md = self.make_md()
        self.registry.append(self.md)
        self.registry.refresh_map()
        run(self.gate.install())
        self.wrapped = self.md.handler

    def test_disabled_session_skips_call(self):
        FakeSp.disabled = {"victim_plugin"}
        invalidate_session_cache()
        event, req = FakeEvent(), object()
        self.assertIsNone(run(self.wrapped(event, req)))
        self.assertIsNone(self.victim.received)  # 目标函数根本没被执行

    def test_enabled_session_passes_args_untouched(self):
        # 只过滤、不改参数：收到的必须就是调用方传进来的那两个对象
        event, req = FakeEvent(), object()
        run(self.wrapped(event, req))
        self.assertEqual(self.victim.received, (event, req))
        self.assertIs(self.victim.received[0], event)
        self.assertIs(self.victim.received[1], req)

    def test_extra_positional_args_still_untouched(self):
        # 闸门绝不"修正"参数：多传就原样交给目标（与旧版对齐逻辑彻底相反）
        async def loose(event, *extra):
            self.victim.received = (event, extra)

        md = self.make_md(full_name="victim.main_loose", handler=loose)
        self.registry.append(md)
        self.registry.refresh_map()
        run(self.gate.install())
        event = FakeEvent()
        run(md.handler(event, "a", "b"))
        self.assertEqual(self.victim.received, (event, ("a", "b")))

    def test_signature_and_coroutine_flag_preserved(self):
        self.assertEqual(
            str(inspect.signature(self.wrapped)),
            str(inspect.signature(self.true_original)),
        )
        self.assertTrue(inspect.iscoroutinefunction(self.wrapped))
        self.assertEqual(self.wrapped.__name__, "on_llm_request")

    def test_asyncgen_handler_passthrough_and_silence(self):
        sink = []
        gen = make_asyncgen_handler(sink)
        md = self.make_md(full_name="victim.main_gen", handler=gen)
        self.registry.append(md)
        self.registry.refresh_map()
        run(self.gate.install())
        self.assertTrue(inspect.isasyncgenfunction(md.handler))

        async def collect(agen):
            return [item async for item in agen]

        # 启用：原样透传（含多余位置参数）
        out = run(collect(md.handler(FakeEvent(), "extra1", "extra2")))
        self.assertEqual(out, ["chunk-1", "chunk-2"])
        self.assertEqual(sink, [("start", 2)])
        # 禁用：一个 yield 都不产出
        sink.clear()
        FakeSp.disabled = {"victim_plugin"}
        invalidate_session_cache()
        self.assertEqual(run(collect(md.handler(FakeEvent()))), [])
        self.assertEqual(sink, [])


class TestInstallLifecycle(BaseCase):
    """安装幂等 / 单层保证 / 死条目中和 / 完整还原。"""

    def test_install_idempotent_single_layer(self):
        md = self.make_md()
        self.registry.append(md)
        self.registry.refresh_map()
        self.assertEqual(run(self.gate.install()), 1)
        first = md.handler
        self.assertEqual(run(self.gate.install()), 0)  # 第二次无事可做
        self.assertIs(md.handler, first)
        self.assertTrue(getattr(first, "_gc_guarded", False))
        self.assertIs(getattr(first, "_gc_orig"), self.true_original)  # 单层：直连原函数

    def test_reload_with_own_stale_wrapper_no_stack(self):
        # 重载后 terminate 没跑到：注册表上残留着自己的旧包装 → 必须解包重装，不叠加
        md = self.make_md()
        self.registry.append(md)
        self.registry.refresh_map()
        run(self.gate.install())
        stale = md.handler
        stale2 = SessionGate()  # 模拟新实例
        run(stale2.install())
        self.assertIs(getattr(md.handler, "_gc_orig"), self.true_original)
        self.assertIsNot(md.handler, stale)

    def test_duplicate_stale_entry_disabled(self):
        stale_md = self.make_md()
        stale_md.handler = object()  # 死实例的旧条目
        latest_md = self.make_md()
        self.registry.extend([stale_md, latest_md])
        self.registry.refresh_map()  # 最新登记的是 latest_md
        added = run(self.gate.install())
        self.assertEqual(added, 1)
        self.assertFalse(stale_md.enabled)  # 死条目停用（不进任何分发路径）
        self.assertTrue(latest_md.enabled)
        self.assertTrue(getattr(latest_md.handler, "_gc_guarded", False))

    def test_skips_self_and_reserved_and_load_events(self):
        own = self.make_md(
            full_name="self.main_h", module_path="self.main",
            handler=self.true_original,
        )
        reserved = self.make_md(
            full_name="reserved.main_h", module_path="reserved.main",
            handler=self.true_original,
        )
        load_ev = self.make_md(handler=self.true_original)
        load_ev.event_type = FakeEventType("OnPluginLoadedEvent")
        self.registry.extend([own, reserved, load_ev])
        self.registry.refresh_map()
        self.assertEqual(run(self.gate.install()), 0)
        for md in (own, reserved, load_ev):
            self.assertFalse(getattr(md.handler, "_gc_guarded", False))

    def test_uninstall_restores_originals(self):
        md = self.make_md()
        tool = FakeTool("victim_tool", self.true_original, "victim.main")
        self.registry.append(md)
        self.registry.refresh_map()
        sys.modules["astrbot.core.provider.register"].llm_tools = types.SimpleNamespace(
            func_list=[tool]
        )
        run(self.gate.install())
        self.assertTrue(getattr(md.handler, "_gc_guarded", False))
        self.assertTrue(getattr(tool.handler, "_gc_guarded", False))
        self.gate.uninstall()
        self.assertIs(md.handler, self.true_original)  # 完整还原
        self.assertIs(tool.handler, self.true_original)

    def test_incremental_install_covers_new_handler(self):
        first = self.make_md()
        self.registry.append(first)
        self.registry.refresh_map()
        run(self.gate.install())
        # 模拟 OnPluginLoadedEvent：新插件随后注册了自己的 handler
        late = self.make_md(full_name="other.main_late", module_path="other.main",
                            handler=self.true_original)
        self.registry.append(late)
        self.registry.refresh_map()
        added = run(self.gate.install())
        self.assertEqual(added, 1)
        self.assertTrue(getattr(late.handler, "_gc_guarded", False))


class TestToolGate(BaseCase):
    """LLM 工具执行路径：直接包装 FunctionTool.handler（与 handler 同一套 _wrap）。"""

    def setUp(self):
        super().setUp()
        self.sink = []
        tool = FakeTool("victim_tool", self._make_handler(), "victim.main")
        self.tool = tool
        self.original_handler = tool.handler
        sys.modules["astrbot.core.provider.register"].llm_tools = types.SimpleNamespace(
            func_list=[tool]
        )
        run(self.gate.install())

    def _make_handler(self, module="victim.main"):
        sink = self.sink

        async def tool_handler(event, arg):
            sink.append(("ran", arg))
            return "ok"

        tool_handler.__module__ = module
        return tool_handler

    def test_disabled_session_blocks_tool(self):
        FakeSp.disabled = {"victim_plugin"}
        invalidate_session_cache()
        result = run(self.tool.handler(FakeEvent(), "x"))
        self.assertIsNone(result)
        self.assertEqual(self.sink, [])  # 工具没执行、没有任何副作用

    def test_enabled_session_passes_through(self):
        result = run(self.tool.handler(FakeEvent(), "x"))
        self.assertEqual(result, "ok")
        self.assertEqual(self.sink, [("ran", "x")])  # 参数原样送达

    def test_stale_patch_not_stacked(self):
        first_wrapper = self.tool.handler  # 本实例已装的包装
        stale = SessionGate()
        run(stale.install())  # 模拟重载后残留的包装
        wrapper = self.tool.handler
        self.assertTrue(getattr(wrapper, "_gc_guarded", False))
        self.assertIs(getattr(wrapper, "_gc_orig"), self.original_handler)  # 仍单层
        stale.uninstall()  # 旧实例只还原"它替换前"的引用（即第一层包装）
        self.assertIs(self.tool.handler, first_wrapper)

    def test_cache_and_invalidate(self):
        FakeSp.disabled = {"victim_plugin"}
        disabled = run(session_disabled_plugins("u1"))
        self.assertEqual(disabled, frozenset({"victim_plugin"}))
        FakeSp.disabled = set()
        self.assertEqual(
            run(session_disabled_plugins("u1")), frozenset({"victim_plugin"})
        )  # TTL 内命中缓存
        invalidate_session_cache()
        self.assertEqual(run(session_disabled_plugins("u1")), frozenset())


class VictimTool:
    """被接管的第三方插件的 LLM 工具方法（形态与 @filter.llm_tool 方法一致）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def tool_handler(self, event, arg) -> str:
        self.calls.append(("ran", arg))
        return "ok"


class TestCoreRebindContract(BaseCase):
    """v3.0 核心回归：核心重跑绑定循环后不得再出现 takes N but M。

    现场日志（用户环境）：
        session_gate.py:321 in guarded -> return await original(*args, **kwargs)
        session_gate.py:321 in guarded -> return await original(*args, **kwargs)
        TypeError: FavourManagerTool._cache_event_for_active_chat()
                   takes 2 positional arguments but 3 were given
    成因是核心把 v2.2 的包装器当成未绑定函数又 partial 了一次实例；
    v3.0 的包装器与 functools.partial(raw, instance) 同形，因此重绑是幂等的。
    """

    def setUp(self):
        super().setUp()
        self.md = self.make_md()
        self.registry.append(self.md)
        self.registry.refresh_map()
        run(self.gate.install())
        self.inst_b = VictimPlugin()  # 模拟"禁用→启用"后的新实例
        self.md.star_cls = self.inst_b

    def assert_single_layer(self):
        # 只有一层闸门：_gc_raw 必须直连真正的原始函数（而不是另一个包装器）。
        # 注意核心用的是"裸" functools.partial（不转发属性），所以要先进 .func。
        handler = self.md.handler
        gate = handler.func if isinstance(handler, functools.partial) else handler
        self.assertIs(getattr(gate, "_gc_raw"), self.true_original.__func__)
        self.assertFalse(getattr(getattr(gate, "_gc_raw", None), "_gc_guarded", False))

    def test_rebind_then_dispatch_keeps_arity(self):
        rebind_like_core(self.md, self.inst_b)
        self.assert_single_layer()
        event, req = FakeEvent(), object()
        run(self.md.handler(event, req))
        self.assertEqual(self.inst_b.received, (event, req))  # 新实例拿到调用
        self.assertIsNone(self.victim.received)  # 旧实例没有被多塞参数地调用

    def test_rebind_then_reinstall_is_idempotent(self):
        rebind_like_core(self.md, self.inst_b)
        before = self.md.handler
        self.assertEqual(run(self.gate.install()), 0)  # 已同形：不再叠层
        self.assertIs(self.md.handler, before)
        self.assert_single_layer()
        event, req = FakeEvent(), object()
        run(self.md.handler(event, req))
        self.assertEqual(self.inst_b.received, (event, req))

    def test_user_scenario_two_stage_does_not_stack(self):
        # 现场时序：install -> 核心重绑 -> OnPluginLoadedEvent 再 install
        rebind_like_core(self.md, self.inst_b)
        run(self.gate.install())
        self.assert_single_layer()
        event, req = FakeEvent(), object()
        run(self.md.handler(event, req))  # 修复前：TypeError takes 2 but 3
        self.assertEqual(self.inst_b.received, (event, req))

    def test_uninstall_after_rebind_restores_core_binding(self):
        rebind_like_core(self.md, self.inst_b)
        run(self.gate.install())  # 刷新还原目标为"核心本来会放的值"
        self.gate.uninstall()
        self.assertFalse(getattr(self.md.handler, "_gc_guarded", False))
        event, req = FakeEvent(), object()
        run(self.md.handler(event, req))
        self.assertEqual(self.inst_b.received, (event, req))

    def test_disabled_session_blocks_after_rebind(self):
        rebind_like_core(self.md, self.inst_b)
        FakeSp.disabled = {"victim_plugin"}
        invalidate_session_cache()
        self.assertIsNone(run(self.md.handler(FakeEvent(), object())))
        self.assertIsNone(self.inst_b.received)

    def test_new_gate_instance_over_rebound_gate_single_layer(self):
        # 旧实例被重载且 terminate 没跑到：新实例重装时必须仍保持单层
        rebind_like_core(self.md, self.inst_b)
        stale = SessionGate()
        run(stale.install())
        self.assert_single_layer()
        event, req = FakeEvent(), object()
        run(self.md.handler(event, req))
        self.assertEqual(self.inst_b.received, (event, req))

    def test_tool_core_rebind_keeps_arity(self):
        # 核心对 ft.handler 走同一套绑定循环（star_manager.py:1279-1303）
        tool_inst = VictimTool()
        tool = FakeTool("victim_tool", tool_inst.tool_handler, "victim.main")
        sys.modules["astrbot.core.provider.register"].llm_tools = types.SimpleNamespace(
            func_list=[tool]
        )
        run(self.gate.install())
        inst_b = VictimTool()
        rebind_like_core(tool, inst_b)
        self.assertEqual(run(tool.handler(FakeEvent(), "x")), "ok")
        self.assertEqual(inst_b.calls, [("ran", "x")])


if __name__ == "__main__":
    unittest.main()
