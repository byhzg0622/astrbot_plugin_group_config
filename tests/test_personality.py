"""人格源（决定性人格）枢纽单元测试：解析、对话对齐、persona_manager wrap。

运行方式（插件根目录下）：python -m unittest discover -s tests -t .
本文件以"插件名包"导入 modules/personality（其相对导入 ..core 需要完整的
包链）。本目录与原版同名（astrbot_plugin_group_config），且工作区里两个目录
并存——为确保导入的是**本目录**的代码，先把本目录注册为同名包（__path__
指向本目录），避免命中旁边的原版目录。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

_pkg = types.ModuleType("astrbot_plugin_group_config")
_pkg.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
sys.modules["astrbot_plugin_group_config"] = _pkg

from astrbot_plugin_group_config.core.apply import SyncEngine  # noqa: E402
from astrbot_plugin_group_config.core.store import PluginStore  # noqa: E402
from astrbot_plugin_group_config.modules.personality import (  # noqa: E402
    PERSONA_FOLLOWERS,
    PersonaSourceHub,
    _active_plugin_module,
)

UMO = "default:GroupMessage:1225425596_1053676331"
UMO_OTHER = "default:GroupMessage:3333333333_1053676331"
CK = "default::group::1053676331"


# ---------------------------------------------------------------------------
# 假实现（形状对齐 AstrBot 真对象用到的面）
# ---------------------------------------------------------------------------
class FakeHooks:
    def __init__(self) -> None:
        self.sp: dict[tuple[str, str], Any] = {}

    async def session_get(self, umo: str, key: str, default: Any) -> Any:
        return self.sp.get((umo, key), default)

    async def session_put(self, umo: str, key: str, value: Any) -> None:
        self.sp[(umo, key)] = value

    async def session_remove(self, umo: str, key: str) -> None:
        self.sp.pop((umo, key), None)

    async def set_provider(self, provider_id: str, umo: str) -> None:
        self.sp[(umo, "provider_perf_chat_completion")] = provider_id

    async def known_umos(self) -> list[str]:
        return []


class FakePersonaManager:
    """persona_manager 假单例。"""

    def __init__(self) -> None:
        self.personas_v3: list[dict[str, Any]] = [
            {"name": "default", "prompt": "base prompt"},
            {"name": "alice", "prompt": "Alice prompt"},
            {"name": "bob", "prompt": "Bob prompt"},
        ]
        self.personas = [
            SimpleNamespace(persona_id=p["name"], system_prompt=p["prompt"])
            for p in self.personas_v3
        ]
        self.default_calls: list[Any] = []
        self.get_calls: list[Any] = []

    async def get_default_persona_v3(self, umo: Any = None) -> Any:
        self.default_calls.append(umo)
        return self.personas_v3[0]

    async def get_persona(self, persona_id: str) -> Any:
        self.get_calls.append(persona_id)
        for p in self.personas:
            if p.persona_id == persona_id:
                return p
        raise ValueError(f"no persona {persona_id}")


class FakeConvManager:
    """conversation_manager 假实现：只有已"创建"的对话。"""

    def __init__(self) -> None:
        self.convs: dict[str, Any] = {}
        self.updates: list[tuple[str, str, str | None]] = []

    def create(self, umo: str, persona_id: str | None = None) -> None:
        self.convs[umo] = SimpleNamespace(persona_id=persona_id)

    async def get_curr_conversation_id(self, umo: str) -> str | None:
        return "conv-1" if umo in self.convs else None

    async def get_conversation(self, umo: str, cid: str) -> Any:
        return self.convs.get(umo)

    async def update_conversation(
        self, umo: str, cid: str, persona_id: str | None = None
    ) -> None:
        self.updates.append((umo, cid, persona_id))
        if umo in self.convs:
            self.convs[umo].persona_id = persona_id


class FakeContext:
    def __init__(self) -> None:
        self.persona_manager = FakePersonaManager()
        self.conversation_manager = FakeConvManager()


def _follower_caller(plugin: str, method: str) -> Any:
    """构造一个"属于某插件模块"的异步调用方（伪造帧的 f_globals）。"""
    src = (
        f"async def _call(pm, *args, **kwargs):\n"
        f"    return await pm.{method}(*args, **kwargs)\n"
    )
    ns: dict[str, Any] = {"__name__": f"{plugin}.core.generator"}
    exec(src, ns)  # noqa: S102 - 测试用：伪造调用方模块名
    return ns["_call"]


class HubBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gcharp-"))
        self.store = PluginStore(self.tmp)
        self.store.load()
        self.hooks = FakeHooks()
        self.engine = SyncEngine(self.store, self.hooks)
        self.context = FakeContext()
        self.hub = PersonaSourceHub(self.store, self.hooks, self.engine, self.context)
        self.pm = self.context.persona_manager
        self.cm = self.context.conversation_manager

    async def asyncTearDown(self):
        self.hub.uninstall()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 决定性人格解析（四层继承 + 勾选框语义）
# ---------------------------------------------------------------------------
class TestDecisivePersona(HubBase):
    def test_off_by_default(self):
        self.store.set_global_scope({"persona": {"persona_id": "alice"}})
        self.assertEqual(self.hub.decisive_persona(UMO), (None, None))

    def test_global_source_on(self):
        self.store.set_global_scope(
            {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        pid, src = self.hub.decisive_persona(UMO)
        self.assertEqual(pid, "alice")
        self.assertEqual(src, "L1:global")
        # 全局决定性人格（UMO 无关生成用）
        self.assertEqual(self.hub.global_decisive(), ("alice", "L1:global"))

    def test_cluster_persona_inherits_global_flag(self):
        # 全局勾选人格源；整群只改人格 → 决定性人格是整群的人格（源头开关继承）
        self.store.set_global_scope(
            {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.store.set_cluster_scope(CK, {"persona": {"persona_id": "bob"}})
        self.assertEqual(self.hub.decisive_persona(UMO)[0], "bob")

    def test_session_off_overrides_global_on(self):
        self.store.set_global_scope(
            {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.store.set_override(UMO, {"persona": {"persona_source": False}})
        self.assertEqual(self.hub.decisive_persona(UMO), (None, None))
        # 同群其他会话不受影响
        self.assertEqual(self.hub.decisive_persona(UMO_OTHER)[0], "alice")

    def test_flag_on_without_persona(self):
        self.store.set_global_scope({"persona": {"persona_source": True}})
        self.assertEqual(self.hub.decisive_persona(UMO), (None, None))
        self.assertEqual(self.hub.global_decisive(), (None, None))

    def test_group_layer_source(self):
        self.store.create_group("a", rules=["default:GroupMessage:*_1053676331"])
        gids = list(self.store.groups())
        self.store.set_group_scope(
            gids[0],
            {"persona": {"persona_id": "alice", "persona_source": True}},
        )
        pid, src = self.hub.decisive_persona(UMO)
        self.assertEqual(pid, "alice")
        self.assertTrue(src.startswith("L2:group:"))


# ---------------------------------------------------------------------------
# 对话人格对齐（主动消息主路径）
# ---------------------------------------------------------------------------
class TestAlignConversation(HubBase):
    async def test_align_existing_conv(self):
        self.store.set_cluster_scope(
            CK, {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.cm.create(UMO, persona_id="bob")
        changed = await self.hub.align_conversation(UMO)
        self.assertTrue(changed)
        self.assertEqual(self.cm.updates, [(UMO, "conv-1", "alice")])
        self.assertEqual(self.cm.convs[UMO].persona_id, "alice")

    async def test_no_write_when_already_aligned(self):
        self.store.set_cluster_scope(
            CK, {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.cm.create(UMO, persona_id="alice")
        self.assertFalse(await self.hub.align_conversation(UMO))
        self.assertEqual(self.cm.updates, [])

    async def test_no_conv_no_create(self):
        self.store.set_cluster_scope(
            CK, {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.assertFalse(await self.hub.align_conversation(UMO))
        self.assertEqual(self.cm.updates, [])
        self.assertNotIn(UMO, self.cm.convs)

    async def test_source_off_no_touch(self):
        self.store.set_cluster_scope(CK, {"persona": {"persona_id": "alice"}})
        self.cm.create(UMO, persona_id="bob")
        self.assertFalse(await self.hub.align_conversation(UMO))
        self.assertEqual(self.cm.updates, [])

    async def test_engine_on_synced_hook(self):
        self.store.set_cluster_scope(
            CK, {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.engine.on_synced = self.hub.after_sync
        self.cm.create(UMO, persona_id=None)
        await self.engine.sync_session(UMO)
        self.assertEqual(self.cm.convs[UMO].persona_id, "alice")

    async def test_persona_source_not_materialized(self):
        # persona_source 是纯标注字段：物化只写 persona_id，不污染原生 sp
        self.store.set_cluster_scope(
            CK, {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        await self.engine.sync_session(UMO)
        svc = self.hooks.sp[(UMO, "session_service_config")]
        self.assertEqual(svc.get("persona_id"), "alice")
        self.assertNotIn("persona_source", svc)

    async def test_flag_only_no_materialize_pollution(self):
        # 只勾人格源、全层无人格：不得往原生 sp 写任何受管字段
        self.store.set_cluster_scope(CK, {"persona": {"persona_source": True}})
        await self.engine.sync_session(UMO)
        svc = self.hooks.sp.get((UMO, "session_service_config"), {})
        self.assertNotIn("persona_id", svc)
        self.assertNotIn("persona_source", svc)

    async def test_store_roundtrip_survives_reload(self):
        self.store.set_global_scope(
            {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        store2 = PluginStore(self.tmp)
        store2.load()
        hub2 = PersonaSourceHub(store2, self.hooks, self.engine, self.context)
        self.assertEqual(hub2.decisive_persona(UMO)[0], "alice")
        self.assertEqual(hub2.global_decisive()[0], "alice")

    async def test_on_synced_exception_isolated(self):
        async def boom(umo: str) -> None:
            raise RuntimeError("callback fail")

        self.engine.on_synced = boom
        rep = await self.engine.sync_session(UMO)  # 不得抛出
        self.assertIsNone(rep.error)


# ---------------------------------------------------------------------------
# persona_manager wrap（动态生活状态等插件的人格选择重定向）
# ---------------------------------------------------------------------------
class TestWraps(HubBase):
    async def test_install_idempotent_and_uninstall(self):
        orig_default = self.pm.get_default_persona_v3
        orig_get = self.pm.get_persona
        self.assertEqual(self.hub.install(), 2)
        self.assertEqual(self.hub.install(), 0)  # 幂等：不叠层
        self.assertTrue(getattr(self.pm.get_persona, "__ps_wrap__", False))
        self.hub.uninstall()
        # 还原为原始绑定方法（函数本体一致）
        self.assertIs(self.pm.get_persona.__func__, orig_get.__func__)
        self.assertIs(self.pm.get_default_persona_v3.__func__, orig_default.__func__)
        self.assertFalse(hasattr(self.pm, "_ps_hub"))

    async def test_follower_get_persona_redirected(self):
        self.store.set_global_scope(
            {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.hub.install()
        follower = _follower_caller("astrbot_plugin_dynamic_life_state", "get_persona")
        got = await follower(self.pm, "default")  # 插件配置里选的是 default
        self.assertEqual(got.system_prompt, "Alice prompt")
        self.assertEqual(self.pm.get_calls, [])  # 原方法未被调用

    async def test_non_follower_get_persona_untouched(self):
        self.store.set_global_scope(
            {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.hub.install()
        got = await self.pm.get_persona("default")  # 本测试模块：非跟随插件
        self.assertEqual(got.system_prompt, "base prompt")
        self.assertEqual(self.pm.get_calls, ["default"])

    async def test_follower_redirect_requires_global_flag(self):
        self.store.set_global_scope({"persona": {"persona_id": "alice"}})  # 未勾选
        self.hub.install()
        follower = _follower_caller("astrbot_plugin_dynamic_life_state", "get_persona")
        got = await follower(self.pm, "default")
        self.assertEqual(got.system_prompt, "base prompt")

    async def test_follower_list_contains_targets(self):
        self.assertIn("astrbot_plugin_dynamic_life_state", PERSONA_FOLLOWERS)
        self.assertIn("astrbot_plugin_proactive_chat", PERSONA_FOLLOWERS)

    async def test_get_default_with_umo_redirects(self):
        # 主动消息回退路径：get_default_persona_v3(umo=...)
        self.store.set_cluster_scope(
            CK, {"persona": {"persona_id": "bob", "persona_source": True}}
        )
        self.hub.install()
        got = await self.pm.get_default_persona_v3(umo=UMO)
        self.assertEqual(got["name"], "bob")
        got2 = await self.pm.get_default_persona_v3(umo="default:FriendMessage:999")
        self.assertEqual(got2["name"], "default")  # 无决定性人格 → 原生

    async def test_get_default_without_umo_uses_global(self):
        self.store.set_global_scope(
            {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.hub.install()
        got = await self.pm.get_default_persona_v3()  # 动态生活状态回退路径
        self.assertEqual(got["name"], "alice")

    async def test_decisive_missing_from_pool_falls_back(self):
        # 决定性人格引用了池中不存在的 id → 回落原方法，不炸
        self.store.set_global_scope(
            {"persona": {"persona_id": "ghost", "persona_source": True}}
        )
        self.hub.install()
        got = await self.pm.get_default_persona_v3()
        self.assertEqual(got["name"], "default")

    async def test_reload_takeover_keeps_routing(self):
        # 本插件重载：新 hub 接管指针；旧 hub 卸载不得拆新包装
        self.store.set_global_scope(
            {"persona": {"persona_id": "alice", "persona_source": True}}
        )
        self.assertEqual(self.hub.install(), 2)
        hub2 = PersonaSourceHub(self.store, self.hooks, self.engine, self.context)
        self.addCleanup(hub2.uninstall)
        self.assertEqual(hub2.install(), 0)  # 复用已有包装，只换指针
        self.assertIs(getattr(self.pm, "_ps_hub"), hub2)
        self.hub.uninstall()  # 旧实例卸载：应完全不动
        self.assertIs(getattr(self.pm, "_ps_hub"), hub2)
        got = await self.pm.get_default_persona_v3()
        self.assertEqual(got["name"], "alice")  # 仍由 hub2 路由

    async def test_unknown_persona_id_still_raises(self):
        self.hub.install()
        with self.assertRaises(ValueError):
            await self.pm.get_persona("nope")  # 非跟随调用方，原样抛错


class TestCallerScan(unittest.TestCase):
    def test_scan_returns_none_in_test_module(self):
        self.assertIsNone(_active_plugin_module())

    def test_scan_finds_follower_module(self):
        fn = _follower_caller("astrbot_plugin_proactive_chat", "get_persona")

        async def run() -> Any:
            return _active_plugin_module()

        import asyncio

        root = asyncio.run(run())
        self.assertIsNone(root)  # run() 帧属于本测试模块
        # 直接在伪造模块里调用扫描函数
        src = (
            "def _scan(helper):\n"
            "    return helper()\n"
        )
        ns: dict[str, Any] = {
            "__name__": "astrbot_plugin_proactive_chat.core.adapter",
            "helper": _active_plugin_module,
        }
        exec(src, ns)  # noqa: S102
        self.assertEqual(ns["_scan"](_active_plugin_module), "astrbot_plugin_proactive_chat")
        self.assertIsNotNone(fn)


if __name__ == "__main__":
    unittest.main()
