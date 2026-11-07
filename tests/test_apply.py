"""SyncEngine 单元测试：物化、漂移检测与 pin、清除恢复、重新接管。

运行方式（插件根目录下）：python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.apply import SyncEngine  # noqa: E402
from core.models import (  # noqa: E402
    KEY_PROVIDER_CHAT,
    KEY_SESSION_PLUGIN,
    KEY_SESSION_SERVICE,
)
from core.store import PluginStore  # noqa: E402

UMO = "default:GroupMessage:1225425596_1053676331"
UMO2 = "default:GroupMessage:2222222222_1053676331"  # 同群另一用户


class FakeHooks:
    """内存版 AstrBot 底层。"""

    def __init__(self, umos: list[str] | None = None) -> None:
        self.sp: dict[tuple[str, str], Any] = {}
        self.provider_calls: list[tuple[str, str]] = []
        self._umos = umos or []

    async def session_get(self, umo: str, key: str, default: Any) -> Any:
        return self.sp.get((umo, key), default)

    async def session_put(self, umo: str, key: str, value: Any) -> None:
        self.sp[(umo, key)] = value

    async def session_remove(self, umo: str, key: str) -> None:
        self.sp.pop((umo, key), None)

    async def set_provider(self, provider_id: str, umo: str) -> None:
        self.provider_calls.append((provider_id, umo))
        self.sp[(umo, KEY_PROVIDER_CHAT)] = provider_id

    async def known_umos(self) -> list[str]:
        return list(self._umos)


class ApplyBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gcapply-"))
        self.store = PluginStore(self.tmp)
        self.store.load()
        self.hooks = FakeHooks()
        self.engine = SyncEngine(self.store, self.hooks)

    async def asyncTearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMaterialize(ApplyBase):
    async def test_service_materialize_and_record(self):
        self.store.set_cluster_scope(
            "default::group::1053676331",
            {"persona": {"persona_id": "P"}, "service": {"llm_enabled": False}},
        )
        rep = await self.engine.sync_session(UMO)
        self.assertIn(KEY_SESSION_SERVICE, rep.wrote)
        self.assertTrue(rep.registered)
        val = self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]
        self.assertEqual(val["persona_id"], "P")
        self.assertIs(val["llm_enabled"], False)
        self.assertEqual(self.store.materialized(UMO)[KEY_SESSION_SERVICE], val)

    async def test_provider_materialize(self):
        self.store.set_override(UMO, {"provider": {"provider_id": "prov-1"}})
        rep = await self.engine.sync_session(UMO)
        self.assertIn(KEY_PROVIDER_CHAT, rep.wrote)
        self.assertEqual(self.hooks.provider_calls, [("prov-1", UMO)])

    async def test_plugins_materialize_structure(self):
        self.store.set_cluster_scope(
            "default::group::1053676331",
            {"plugins": {"disabled_plugins": ["bad_plugin"]}},
        )
        await self.engine.sync_session(UMO)
        val = self.hooks.sp[(UMO, KEY_SESSION_PLUGIN)]
        self.assertEqual(val[UMO]["disabled_plugins"], ["bad_plugin"])

    async def test_baseline_preserved_on_first_write(self):
        # 官方页面已有设置（无物化记录）：只叠加受管字段，其余保留
        self.hooks.sp[(UMO, KEY_SESSION_SERVICE)] = {"custom_name": "昵称"}
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        await self.engine.sync_session(UMO)
        val = self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]
        self.assertEqual(val["custom_name"], "昵称")
        self.assertEqual(val["persona_id"], "P")


class TestDrift(ApplyBase):
    async def test_external_edit_is_overwritten_no_pin(self):
        """外部改动不会再触发"手动改过"标记：同步一律以定义层配置为准。"""
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        await self.engine.sync_session(UMO)
        # 外部（官方页面）改了受管字段
        self.hooks.sp[(UMO, KEY_SESSION_SERVICE)] = {"persona_id": "HAND"}
        rep = await self.engine.sync_session(UMO)
        # 不打标，且配置被定义层覆盖回来
        self.assertEqual(rep.drift, [])
        self.assertFalse(self.store.is_pinned(UMO, KEY_SESSION_SERVICE))
        self.assertEqual(
            self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]["persona_id"], "P"
        )

    async def test_unmanaged_field_change_is_not_drift(self):
        """官方自动命名写 custom_name 不应被误判为外部手改（全员 pin 的根因）。"""
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        await self.engine.sync_session(UMO)
        # 外部往同一 dict 追加官方字段（模拟自动命名）
        cur = self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]
        self.hooks.sp[(UMO, KEY_SESSION_SERVICE)] = {**cur, "custom_name": "小可爱"}
        rep = await self.engine.sync_session(UMO)
        self.assertEqual(rep.drift, [])
        self.assertFalse(self.store.is_pinned(UMO, KEY_SESSION_SERVICE))
        # 再同步一次仍不漂移，且官方字段没被冲掉
        rep2 = await self.engine.sync_session(UMO)
        self.assertEqual(rep2.drift, [])
        self.assertEqual(
            self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]["custom_name"], "小可爱"
        )

    async def test_history_pin_cleared_on_sync(self):
        """历史遗留的"手动改过"标记在同步时被清除，会话重新跟随定义层。"""
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        self.store.add_pin(UMO, KEY_SESSION_SERVICE)
        rep = await self.engine.sync_session(UMO)
        self.assertFalse(self.store.is_pinned(UMO, KEY_SESSION_SERVICE))
        self.assertEqual(
            self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]["persona_id"], "P"
        )
        self.assertNotIn(KEY_SESSION_SERVICE, rep.pinned_skipped)

    async def test_takeover_reapplies_definition(self):
        """历史 pin + 单独设置 → takeover 后重新按定义层下发。"""
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        await self.engine.sync_session(UMO)
        # 制造历史状态：pin + 单独设置
        self.store.add_pin(UMO, KEY_SESSION_SERVICE)
        self.store.set_override(UMO, {"persona": {"persona_id": "OWN"}})
        # takeover 式清理
        self.engine.takeover(UMO)
        self.store.clear_override(UMO)
        self.store.clear_materialized(UMO)
        self.hooks.sp.pop((UMO, KEY_SESSION_SERVICE), None)
        rep = await self.engine.sync_session(UMO)
        self.assertFalse(self.store.is_pinned(UMO, KEY_SESSION_SERVICE))
        self.assertEqual(
            self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]["persona_id"], "P"
        )


class TestClearAndBatch(ApplyBase):
    async def test_clear_restores_native(self):
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        await self.engine.sync_session(UMO)
        self.assertIn((UMO, KEY_SESSION_SERVICE), self.hooks.sp)
        # 删除整群设定 → 解析为空 → 清除物化
        self.store.delete_cluster("default::group::1053676331")
        rep = await self.engine.sync_session(UMO)
        self.assertIn(KEY_SESSION_SERVICE, rep.removed)
        self.assertNotIn((UMO, KEY_SESSION_SERVICE), self.hooks.sp)
        self.assertNotIn(KEY_SESSION_SERVICE, self.store.materialized(UMO))

    async def test_clear_keeps_official_fields(self):
        """清除时只摘受管字段，官方字段（custom_name）必须保留。"""
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        await self.engine.sync_session(UMO)
        cur = self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]
        self.hooks.sp[(UMO, KEY_SESSION_SERVICE)] = {**cur, "custom_name": "小可爱"}
        self.store.delete_cluster("default::group::1053676331")
        rep = await self.engine.sync_session(UMO)
        self.assertIn(KEY_SESSION_SERVICE, rep.removed)
        # key 仍在，只剩官方字段；persona_id 已被摘掉
        self.assertEqual(
            self.hooks.sp[(UMO, KEY_SESSION_SERVICE)], {"custom_name": "小可爱"}
        )

    async def test_clear_works_even_without_record(self):
        """materialized 记录被清掉（如完全恢复原状）后，恢复自动仍能摘掉残值。

        否则原生层残值会永远显示"当前实际"，把"自动沿用"顶掉。
        """
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        await self.engine.sync_session(UMO)
        self.store.clear_materialized(UMO)            # 模拟记录丢失
        self.store.delete_cluster("default::group::1053676331")
        rep = await self.engine.sync_session(UMO)
        self.assertIn(KEY_SESSION_SERVICE, rep.removed)
        self.assertNotIn((UMO, KEY_SESSION_SERVICE), self.hooks.sp)

    async def test_persona_residual_removed_when_service_present(self):
        """persona 全层无值但 service 有值时，persona 残值必须被摘掉。

        人格与服务共用 session_service_config——旧实现 dict(current) 拷贝
        再覆盖，会让 persona 残值搭车存活（恢复自动后旧人格弹回）。
        """
        # 先物化一个单独人格
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "X"}}
        )
        await self.engine.sync_session(UMO)
        self.assertEqual(
            self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]["persona_id"], "X"
        )
        # 改为：persona 全层清空，但 service 类别仍有定义值
        self.store.delete_cluster("default::group::1053676331")
        self.store.set_override(UMO, {"service": {"llm_enabled": False}})
        rep = await self.engine.sync_session(UMO)
        cur = self.hooks.sp[(UMO, KEY_SESSION_SERVICE)]
        self.assertNotIn("persona_id", cur)            # 人格残值被摘掉
        self.assertIs(cur["llm_enabled"], False)        # service 保留
        self.assertIn(KEY_SESSION_SERVICE, rep.wrote)

    async def test_reconcile_covers_bottom_up_known(self):
        self.hooks._umos = [UMO, UMO2]
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        reps = await self.engine.reconcile_all()
        self.assertEqual(len(reps), 2)
        for rep in reps:
            self.assertIn(KEY_SESSION_SERVICE, rep.wrote)
        self.assertEqual(self.store.all_umos(), sorted([UMO, UMO2]))

    async def test_error_isolated_per_session(self):
        class BoomHooks(FakeHooks):
            async def session_put(self, umo, key, value):
                raise RuntimeError("boom")

        engine = SyncEngine(self.store, BoomHooks())
        self.store.set_cluster_scope(
            "default::group::1053676331", {"persona": {"persona_id": "P"}}
        )
        rep = await engine.sync_session(UMO)
        self.assertIsNotNone(rep.error)
        self.assertIn("boom", rep.error or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
