"""core 层单元测试：UMO 解析、分组与补集、五层继承解析、存储与校验。

运行方式（插件根目录下）：python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.grouping import (  # noqa: E402
    PriorityConflictError,
    complement,
    find_priority_conflict,
    group_matches,
    matching_groups,
    rule_matches,
)
from core.models import (  # noqa: E402
    CATEGORY_PERSONA,
    CATEGORY_PLUGINS,
    CATEGORY_PROVIDER,
    CATEGORY_SERVICE,
    cluster_key,
    cluster_rule_patterns,
    empty_scope,
    is_group_sub_session,
    merge_scope,
    normalize_scope,
    parse_umo,
    prune_scope,
    sub_session_user,
)
from core.resolver import resolve  # noqa: E402
from core.store import PluginStore, StoreError  # noqa: E402

# UMO 实测形态（用户确认）：default:GroupMessage:本人QQ号_群号
UMO_SUB = "default:GroupMessage:1225425596_1053676331"  # 用户1225425596 在群1053676331
UMO_BARE = "default:GroupMessage:1053676331"  # 群1053676331 的裸群会话（与 UMO_SUB 同群）
UMO_OTHER = "default:GroupMessage:151291050_1038186906"  # 另一个群 1038186906
UMO_PRIVATE = "default:FriendMessage:1053676331"  # 私聊
UMO_WEBCHAT = "webchat:FriendMessage:webchat-abc"  # webchat 私聊


class TestUmo(unittest.TestCase):
    def test_parse(self):
        p = parse_umo(UMO_SUB)
        assert p is not None
        self.assertEqual(p.platform, "default")
        self.assertEqual(p.message_type, "GroupMessage")
        self.assertEqual(p.session_id, "1225425596_1053676331")
        self.assertIsNone(parse_umo("bad"))
        self.assertIsNone(parse_umo(123))  # type: ignore[arg-type]

    def test_cluster_key(self):
        # 群号在 "_" 之后：1225425596_1053676331 → 群 1053676331
        self.assertEqual(cluster_key(UMO_SUB), "default::group::1053676331")
        # 子会话与裸群 UMO 归到同一物理群
        self.assertEqual(cluster_key(UMO_SUB), cluster_key(UMO_BARE))
        # 裸群会话整段即群号
        self.assertEqual(cluster_key("default:GroupMessage:1225425596"), "default::group::1225425596")
        # 另一个群
        self.assertEqual(cluster_key(UMO_OTHER), "default::group::1038186906")
        # 私聊与 webchat 不提取
        self.assertIsNone(cluster_key(UMO_PRIVATE))
        self.assertIsNone(cluster_key(UMO_WEBCHAT))

    def test_sub_session_user(self):
        self.assertEqual(sub_session_user(UMO_SUB), "1225425596")
        self.assertIsNone(sub_session_user(UMO_BARE))
        self.assertIsNone(sub_session_user(UMO_PRIVATE))

    def test_sub_session(self):
        self.assertTrue(is_group_sub_session(UMO_SUB))
        self.assertFalse(is_group_sub_session(UMO_BARE))
        self.assertFalse(is_group_sub_session(UMO_PRIVATE))


class TestScope(unittest.TestCase):
    def test_normalize_drops_invalid(self):
        scope = normalize_scope(
            {
                "persona": {"persona_id": "alice", "junk": 1},
                "provider": {"provider_id": ""},
                "service": {"llm_enabled": "yes"},  # 非 bool，丢弃
                "plugins": {"disabled_plugins": ["p1", 2], "unknown": []},
                "bogus": {"x": 1},
            }
        )
        self.assertEqual(scope["persona"], {"persona_id": "alice"})
        self.assertEqual(scope["provider"], {})
        self.assertEqual(scope["service"], {})
        self.assertEqual(scope["plugins"]["disabled_plugins"], ["p1", "2"])
        self.assertNotIn("bogus", scope)

    def test_merge_field_level(self):
        base = normalize_scope({"persona": {"persona_id": "a"}, "service": {"llm_enabled": True}})
        over = normalize_scope({"persona": {"persona_id": "b"}})
        merged = merge_scope(base, over)
        self.assertEqual(merged["persona"]["persona_id"], "b")
        self.assertTrue(merged["service"]["llm_enabled"])

    def test_prune(self):
        scope = normalize_scope({"persona": {"persona_id": "a"}, "service": {"llm_enabled": True}})
        only = prune_scope(scope, [CATEGORY_PERSONA])
        self.assertTrue(only["persona"])
        self.assertEqual(only["service"], {})


class TestGrouping(unittest.TestCase):
    def test_rule_match(self):
        # 按用户通配（前缀 = 某人的全部会话）
        self.assertTrue(rule_matches("default:GroupMessage:1225425596_*", UMO_SUB))
        self.assertFalse(rule_matches("default:GroupMessage:1225425596_*", UMO_BARE))
        # 按群通配（后缀 = 某群的全部子会话）
        self.assertTrue(rule_matches("default:GroupMessage:*_1053676331", UMO_SUB))
        self.assertFalse(rule_matches("default:GroupMessage:*_1053676331", UMO_OTHER))
        self.assertTrue(rule_matches("default:GroupMessage:*", UMO_BARE))
        # 整群规则 = 裸 + 子会话两条；盖住同群、盖不住别群
        pats = cluster_rule_patterns("default", "1053676331")
        self.assertTrue(any(rule_matches(p, UMO_BARE) for p in pats))
        self.assertTrue(any(rule_matches(p, UMO_SUB) for p in pats))
        self.assertFalse(any(rule_matches(p, UMO_OTHER) for p in pats))

    def test_priority_ordering_and_complement(self):
        groups = {
            "low": {
                "name": "a", "priority": 1, "members": [UMO_SUB], "rules": [],
                "scope": {}, "created_at": 1,
            },
            "high": {
                "name": "b", "priority": 2, "members": [UMO_OTHER], "rules": [],
                "scope": {}, "created_at": 2,
            },
            "rule": {
                "name": "c", "priority": 3, "members": [],
                "rules": ["default:GroupMessage:*_1053676331"],
                "scope": {}, "created_at": 3,
            },
        }
        from core.models import Group

        gs = {gid: Group.from_dict(gid, raw) for gid, raw in groups.items()}
        hits = matching_groups(gs, UMO_SUB)
        # UMO_SUB：low(1) 与 rule(3) 都命中，按 priority 升序
        self.assertEqual([g.id for g in hits], ["low", "rule"])
        self.assertTrue(group_matches(gs["rule"], UMO_SUB))
        self.assertFalse(group_matches(gs["high"], UMO_SUB))

        all_umos = [UMO_SUB, UMO_BARE, UMO_OTHER]
        # 补集：排除 rule(通配群1053676331子会话 → 命中 UMO_SUB) 与 high(UMO_OTHER) 的并集
        comp = complement(all_umos, [gs["rule"], gs["high"]])
        self.assertEqual(comp, [UMO_BARE])

    def test_priority_conflict(self):
        from core.models import Group

        g1 = Group(id="g1", name="a", priority=1, members=[UMO_SUB], created_at=1)
        g2 = Group(id="g2", name="b", priority=1, members=[], created_at=2)
        g3 = Group(id="g3", name="c", priority=2, members=[], created_at=3)
        all_groups = {"g1": g1, "g2": g2, "g3": g3}
        # 同优先级冲突：g2 与 g1 同为 priority=1，g1 已持有 UMO_SUB
        conflict = find_priority_conflict(all_groups, UMO_SUB, g2)
        self.assertEqual(conflict.id if conflict else None, "g1")
        # 不同优先级允许
        conflict2 = find_priority_conflict(all_groups, UMO_SUB, g3)
        self.assertIsNone(conflict2)


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.data = {
            "global_scope": normalize_scope(
                {"persona": {"persona_id": "G"}, "service": {"llm_enabled": True}}
            ),
            "groups": {
                "ga": {
                    "name": "a", "priority": 1, "members": [UMO_SUB], "rules": [],
                    "scope": normalize_scope({"persona": {"persona_id": "A"}}),
                    "created_at": 1,
                },
                "gc": {
                    "name": "c", "priority": 3, "members": [],
                    "rules": ["default:GroupMessage:*_1053676331"],
                    "scope": normalize_scope({"persona": {"persona_id": "C"}}),
                    "created_at": 2,
                },
            },
            "clusters": {
                "default::group::1053676331": {
                    "scope": normalize_scope({"persona": {"persona_id": "CLUSTER"}})
                },
            },
            "overrides": {
                UMO_SUB: normalize_scope({"persona": {"persona_id": "OWN"}}),
            },
        }

    def test_layer_order(self):
        # UMO_SUB：override(L4) 赢过 cluster(L3) 赢过 group(最高优先级组 L2) 赢过 global
        res = resolve(self.data, UMO_SUB)
        self.assertEqual(res.value(CATEGORY_PERSONA, "persona_id"), "OWN")
        self.assertEqual(res.source(CATEGORY_PERSONA, "persona_id"), "L4:session")
        # L2 中命中的是 priority=3 的规则组 gc（覆盖 ga）
        self.assertEqual(res.matched_groups, ["ga", "gc"])
        # 未被覆盖的字段来自 L1
        self.assertEqual(res.value(CATEGORY_SERVICE, "llm_enabled"), True)
        self.assertEqual(res.source(CATEGORY_SERVICE, "llm_enabled"), "L1:global")

    def test_cluster_wins_over_group(self):
        # 无 override 的同群子会话：cluster 赢过 group
        umo2 = "default:GroupMessage:2222222222_1053676331"  # 同群另一用户
        res = resolve(self.data, umo2)
        self.assertEqual(res.value(CATEGORY_PERSONA, "persona_id"), "CLUSTER")
        self.assertEqual(res.source(CATEGORY_PERSONA, "persona_id").split(":")[0], "L3")
        self.assertEqual(res.matched_groups, ["ga", "gc"] if umo2 in ["x"] else ["gc"])

    def test_ungrouped_layer(self):
        """未命中任何分组的会话应用"未分类层"；命中分组的不应用。"""
        data = dict(self.data)
        data["ungrouped_scope"] = normalize_scope(
            {"persona": {"persona_id": "U"}}
        )
        # UMO_OTHER 不在任何分组 → 未分类层盖过全局
        res = resolve(data, UMO_OTHER)
        self.assertEqual(res.value(CATEGORY_PERSONA, "persona_id"), "U")
        self.assertEqual(
            res.source(CATEGORY_PERSONA, "persona_id"),
            "L2:group:__ungrouped__",
        )
        self.assertEqual(res.matched_groups, [])
        # UMO_SUB 命中分组 → 不应用未分类层，分组配置优先
        res2 = resolve(data, UMO_SUB)
        self.assertNotEqual(res2.value(CATEGORY_PERSONA, "persona_id"), "U")

    def test_unrelated_session_uses_global(self):
        res = resolve(self.data, UMO_OTHER)
        self.assertEqual(res.value(CATEGORY_PERSONA, "persona_id"), "G")
        self.assertEqual(res.source(CATEGORY_PERSONA, "persona_id"), "L1:global")
        self.assertEqual(res.matched_groups, [])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gcstore-"))
        self.store = PluginStore(self.tmp)
        self.store.load()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip(self):
        self.store.register_session(UMO_SUB, "db")
        g = self.store.create_group("a", priority=1)
        self.store.add_member(g.id, UMO_SUB)
        self.store.set_group_scope(g.id, {"persona": {"persona_id": "A"}})
        # 重新加载
        store2 = PluginStore(self.tmp)
        store2.load()
        g2 = store2.get_group(g.id)
        self.assertEqual(g2.members, [UMO_SUB])
        self.assertEqual(g2.scope["persona"]["persona_id"], "A")
        self.assertEqual(store2.all_umos(), [UMO_SUB])

    def test_priority_conflict_enforced(self):
        g1 = self.store.create_group("a", priority=1)
        g2 = self.store.create_group("b", priority=1)
        self.store.add_member(g1.id, UMO_SUB)
        with self.assertRaises(PriorityConflictError):
            self.store.add_member(g2.id, UMO_SUB)
        # 不同优先级允许
        g3 = self.store.create_group("c", priority=2)
        self.store.add_member(g3.id, UMO_SUB)
        self.assertEqual(len(self.store.get_group(g3.id).members), 1)
        # 改成同优先级也要拒绝
        with self.assertRaises(PriorityConflictError):
            self.store.update_group(g3.id, priority=1)

    def test_override_clear(self):
        self.store.set_override(UMO_SUB, {"persona": {"persona_id": "OWN"}})
        self.assertEqual(self.store.override(UMO_SUB)["persona"]["persona_id"], "OWN")
        self.store.clear_override(UMO_SUB, [CATEGORY_PERSONA])
        self.assertEqual(self.store.override(UMO_SUB), empty_scope())

    def test_materialized_and_pin(self):
        self.store.record_materialized(UMO_SUB, "session_service_config", {"persona_id": "P"})
        self.assertEqual(
            self.store.materialized(UMO_SUB)["session_service_config"]["persona_id"],
            "P",
        )
        self.assertFalse(self.store.is_pinned(UMO_SUB, "session_service_config"))
        self.store.add_pin(UMO_SUB, "session_service_config")
        self.assertTrue(self.store.is_pinned(UMO_SUB, "session_service_config"))
        self.store.remove_pin(UMO_SUB, "session_service_config")
        self.assertFalse(self.store.is_pinned(UMO_SUB, "session_service_config"))

    def test_snapshot_restore(self):
        self.store.set_global_scope({"persona": {"persona_id": "G1"}})
        snap = self.store.snapshot("test")
        self.store.set_global_scope({"persona": {"persona_id": "G2"}}, replace=True)
        self.assertEqual(self.store.global_scope()["persona"]["persona_id"], "G2")
        self.store.restore_snapshot(snap.name)
        self.assertEqual(self.store.global_scope()["persona"]["persona_id"], "G1")

    def test_replace_vs_merge(self):
        self.store.set_global_scope({"service": {"llm_enabled": True}})
        self.store.set_global_scope({"service": {"tts_enabled": False}})
        # 合并式：两个字段共存
        svc = self.store.global_scope()["service"]
        self.assertTrue(svc["llm_enabled"])
        self.assertFalse(svc["tts_enabled"])
        # 覆盖式：该类被整体替换
        self.store.set_global_scope({"service": {"session_enabled": True}}, replace=True)
        svc = self.store.global_scope()["service"]
        self.assertEqual(svc, {"session_enabled": True})

    def test_missing_group_raises(self):
        with self.assertRaises(StoreError):
            self.store.get_group("nope")


class TestEndToEnd(unittest.TestCase):
    """验收用例 1-4、6 的可自动化部分（对应 DESIGN 4.3）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gce2e-"))
        self.store = PluginStore(self.tmp)
        self.store.load()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_case1_new_subsession_inherits_cluster(self):
        # 整群设人格 P
        self.store.set_cluster_scope("default::group::1053676331", {"persona": {"persona_id": "P"}})
        res = resolve(self.store.data, UMO_SUB)
        self.assertEqual(res.value(CATEGORY_PERSONA, "persona_id"), "P")
        # 新出现的子会话同样继承（无需登记）
        res2 = resolve(self.store.data, "default:GroupMessage:9999999999_1053676331")
        self.assertEqual(res2.value(CATEGORY_PERSONA, "persona_id"), "P")

    def test_case2_subsession_override_isolation(self):
        self.store.set_cluster_scope("default::group::1053676331", {"persona": {"persona_id": "P"}})
        self.store.set_override(UMO_SUB, {"persona": {"persona_id": "Q"}})
        res = resolve(self.store.data, UMO_SUB)
        self.assertEqual(res.value(CATEGORY_PERSONA, "persona_id"), "Q")
        res2 = resolve(self.store.data, "default:GroupMessage:8888888888_1053676331")
        self.assertEqual(res2.value(CATEGORY_PERSONA, "persona_id"), "P")

    def test_case3_clear_override_restores_inheritance(self):
        self.store.set_cluster_scope("default::group::1053676331", {"persona": {"persona_id": "P"}})
        self.store.set_override(UMO_SUB, {"persona": {"persona_id": "Q"}})
        self.store.clear_override(UMO_SUB)
        res = resolve(self.store.data, UMO_SUB)
        self.assertEqual(res.value(CATEGORY_PERSONA, "persona_id"), "P")
        self.assertEqual(res.source(CATEGORY_PERSONA, "persona_id"), "L3:cluster:default::group::1053676331")

    def test_case4_complement_batch(self):
        # 全集三会话，分组 a 含群 1053676331（整群规则）
        for umo in (UMO_SUB, UMO_BARE, UMO_OTHER):
            self.store.register_session(umo, "db")
        # 整群入组：裸 + 子会话两条规则，覆盖群 1053676331 全部会话
        g = self.store.create_group(
            "a", priority=1, rules=cluster_rule_patterns("default", "1053676331")
        )
        groups = self.store.groups()
        comp = complement(self.store.all_umos(), [groups[g.id]])
        # UMO_SUB、UMO_BARE 同属群 1053676331 被排除；UMO_OTHER 属群 1038186906 → 补集
        self.assertEqual(comp, [UMO_OTHER])
        # 对补集写覆盖，a 内不受影响
        for umo in comp:
            self.store.set_override(umo, {"provider": {"provider_id": "M"}})
        res_a = resolve(self.store.data, UMO_SUB)
        res_out = resolve(self.store.data, UMO_OTHER)
        self.assertIsNone(res_a.value(CATEGORY_PROVIDER, "provider_id"))
        self.assertEqual(res_out.value(CATEGORY_PROVIDER, "provider_id"), "M")

    def test_case6_priority_mutual_exclusion(self):
        g1 = self.store.create_group("a", priority=1)
        g2 = self.store.create_group("b", priority=1)
        self.store.add_member(g1.id, UMO_SUB)
        with self.assertRaises(PriorityConflictError):
            self.store.add_member(g2.id, UMO_SUB)


if __name__ == "__main__":
    unittest.main(verbosity=2)
