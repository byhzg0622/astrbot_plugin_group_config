"""定义层持久化（纯 Python，不依赖 AstrBot 运行时）。

对应 docs/DESIGN.md 第 7 节：store.json（主存储）、registry.json（全集）、
snapshots/（批量前快照）。写盘采用临时文件 + os.replace 原子替换。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .grouping import PriorityConflictError, find_priority_conflict
from .models import (
    SCHEMA_VERSION,
    Group,
    empty_scope,
    normalize_scope,
)


class StoreError(RuntimeError):
    """存储层错误（找不到分组等）。"""


class PluginStore:
    """定义层存储：分组/整群/子会话覆盖/物化记录/快照。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store_path = self.data_dir / "store.json"
        self.registry_path = self.data_dir / "registry.json"
        self.snapshot_dir = self.data_dir / "snapshots"
        self.data: dict[str, Any] = self._default_data()
        self.registry: dict[str, Any] = {"sessions": {}}

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------
    @staticmethod
    def _default_data() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "global_scope": empty_scope(),
            "ungrouped_scope": empty_scope(),
            "groups": {},
            "clusters": {},
            "overrides": {},
            "pins": {},
            "materialized": {},
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def load(self) -> None:
        """读盘；文件缺失/损坏时回落默认结构（损坏文件先备份，不覆盖原始现场）。"""
        raw = self._read_json(self.store_path)
        if not raw and self.store_path.exists():
            # 存在但读不出来（损坏）：备份一份，避免静默覆盖
            try:
                os.replace(
                    self.store_path,
                    self.store_path.with_name("store.json.corrupt"),
                )
            except OSError:
                pass
        data = self._default_data()
        if raw:
            data["global_scope"] = normalize_scope(raw.get("global_scope"))
            data["ungrouped_scope"] = normalize_scope(raw.get("ungrouped_scope"))
            data["groups"] = raw.get("groups") or {}
            data["clusters"] = raw.get("clusters") or {}
            data["overrides"] = raw.get("overrides") or {}
            data["pins"] = raw.get("pins") or {}
            data["materialized"] = raw.get("materialized") or {}
            data["schema_version"] = int(raw.get("schema_version", SCHEMA_VERSION))
        self.data = data
        reg = self._read_json(self.registry_path)
        self.registry = {"sessions": reg.get("sessions") or {}}

    def save(self) -> None:
        """原子保存主存储。"""
        self._write_json_atomic(self.store_path, self.data)

    def save_registry(self) -> None:
        """原子保存全集登记簿。"""
        self._write_json_atomic(self.registry_path, self.registry)

    # ------------------------------------------------------------------
    # 全集（registry）
    # ------------------------------------------------------------------
    def all_umos(self) -> list[str]:
        return sorted(self.registry.get("sessions", {}).keys())

    def register_session(self, umo: str, source: str = "event") -> bool:
        """登记一个 UMO（立即持久化）；返回是否为新发现。"""
        sessions = self.registry.setdefault("sessions", {})
        if umo in sessions:
            return False
        sessions[umo] = {"first_seen": int(time.time()), "source": source}
        self.save_registry()
        return True

    def register_many(self, umos: list[str], source: str = "db") -> int:
        """批量登记（一次持久化），返回新增数量。"""
        sessions = self.registry.setdefault("sessions", {})
        added = 0
        now = int(time.time())
        for umo in umos:
            if umo not in sessions:
                sessions[umo] = {"first_seen": now, "source": source}
                added += 1
        if added:
            self.save_registry()
        return added

    def forget_session(self, umo: str) -> None:
        self.registry.get("sessions", {}).pop(umo, None)

    # ------------------------------------------------------------------
    # 全局层（L1）
    # ------------------------------------------------------------------
    def global_scope(self) -> dict[str, dict[str, Any]]:
        return normalize_scope(self.data.get("global_scope"))

    def set_global_scope(
        self,
        delta: dict[str, Any],
        *,
        replace: bool = False,
        categories: list[str] | None = None,
    ) -> None:
        self.data["global_scope"] = self._apply_delta(
            self.global_scope(), delta, replace, categories
        )
        self.save()

    # ------------------------------------------------------------------
    # 未分类层（未命中任何分组的会话使用；介于全局与分组之间）
    # ------------------------------------------------------------------
    def ungrouped_scope(self) -> dict[str, dict[str, Any]]:
        return normalize_scope(self.data.get("ungrouped_scope"))

    def set_ungrouped_scope(
        self,
        delta: dict[str, Any],
        *,
        replace: bool = False,
        categories: list[str] | None = None,
    ) -> None:
        self.data["ungrouped_scope"] = self._apply_delta(
            self.ungrouped_scope(), delta, replace, categories
        )
        self.save()

    # ------------------------------------------------------------------
    # 分组（L2）
    # ------------------------------------------------------------------
    def groups(self) -> dict[str, Group]:
        return {
            gid: Group.from_dict(gid, raw)
            for gid, raw in self.data.get("groups", {}).items()
        }

    def get_group(self, gid: str) -> Group:
        raw = self.data.get("groups", {}).get(gid)
        if raw is None:
            raise StoreError(f"分组 '{gid}' 不存在")
        return Group.from_dict(gid, raw)

    def create_group(
        self,
        name: str,
        *,
        priority: int = 100,
        members: list[str] | None = None,
        rules: list[str] | None = None,
        scope: dict[str, Any] | None = None,
    ) -> Group:
        gid = f"g{int(time.time() * 1000) % 10_000_000:07d}"
        while gid in self.data.get("groups", {}):
            gid = f"g{int(time.time() * 1000) % 10_000_000:07d}"
        group = Group(
            id=gid,
            name=name,
            priority=priority,
            members=list(members or []),
            rules=list(rules or []),
            scope=normalize_scope(scope),
            created_at=int(time.time()),
        )
        # 初始成员同样受同优先级互斥约束
        for umo in list(group.members):
            conflict = find_priority_conflict(self.groups(), umo, group)
            if conflict:
                raise PriorityConflictError(umo, conflict)
        self.data.setdefault("groups", {})[gid] = group.to_dict()
        self.save()
        return group

    def update_group(
        self,
        gid: str,
        *,
        name: str | None = None,
        priority: int | None = None,
        scope: dict[str, Any] | None = None,
        replace_scope: bool = False,
    ) -> Group:
        group = self.get_group(gid)
        if name is not None:
            group.name = name
        if priority is not None:
            # 先临时采用新优先级，再按新优先级重新校验互斥；有冲突则回滚并拒绝
            old_priority = group.priority
            group.priority = priority
            for umo in group.members:
                conflict = find_priority_conflict(self.groups(), umo, group)
                if conflict:
                    group.priority = old_priority
                    raise PriorityConflictError(umo, conflict)
        if scope is not None:
            if replace_scope:
                group.scope = normalize_scope(scope)
            else:
                group.scope = normalize_scope(
                    {
                        c: {**group.scope.get(c, {}), **scope.get(c, {})}
                        for c in group.scope
                    }
                )
        self.data["groups"][gid] = group.to_dict()
        self.save()
        return group

    def delete_group(self, gid: str) -> None:
        self.get_group(gid)
        del self.data["groups"][gid]
        self.save()

    def add_member(self, gid: str, umo: str) -> None:
        """加入显式成员；同优先级重复归属直接拒绝。"""
        group = self.get_group(gid)
        if umo in group.members:
            return
        conflict = find_priority_conflict(self.groups(), umo, group)
        if conflict:
            raise PriorityConflictError(umo, conflict)
        group.members.append(umo)
        self.data["groups"][gid] = group.to_dict()
        self.save()

    def remove_member(self, gid: str, umo: str) -> None:
        group = self.get_group(gid)
        if umo not in group.members:
            return
        group.members.remove(umo)
        self.data["groups"][gid] = group.to_dict()
        self.save()

    def add_rule(self, gid: str, rule: str) -> None:
        group = self.get_group(gid)
        if rule not in group.rules:
            group.rules.append(rule)
            self.data["groups"][gid] = group.to_dict()
            self.save()

    def remove_rule(self, gid: str, rule: str) -> None:
        group = self.get_group(gid)
        if rule in group.rules:
            group.rules.remove(rule)
            self.data["groups"][gid] = group.to_dict()
            self.save()

    def set_group_scope(
        self,
        gid: str,
        delta: dict[str, Any],
        *,
        replace: bool = False,
        categories: list[str] | None = None,
    ) -> None:
        group = self.get_group(gid)
        group.scope = self._apply_delta(group.scope, delta, replace, categories)
        self.data["groups"][gid] = group.to_dict()
        self.save()

    # ------------------------------------------------------------------
    # 整群（L3）
    # ------------------------------------------------------------------
    def cluster_scope(self, ck: str) -> dict[str, dict[str, Any]]:
        raw = self.data.get("clusters", {}).get(ck)
        return normalize_scope((raw or {}).get("scope"))

    def set_cluster_scope(
        self,
        ck: str,
        delta: dict[str, Any],
        *,
        replace: bool = False,
        categories: list[str] | None = None,
    ) -> None:
        clusters = self.data.setdefault("clusters", {})
        entry = clusters.setdefault(ck, {"scope": empty_scope()})
        entry["scope"] = self._apply_delta(
            normalize_scope(entry.get("scope")), delta, replace, categories
        )
        self.save()

    def delete_cluster(self, ck: str) -> None:
        if ck in self.data.get("clusters", {}):
            del self.data["clusters"][ck]
            self.save()

    # ------------------------------------------------------------------
    # 子会话覆盖（L4）
    # ------------------------------------------------------------------
    def override(self, umo: str) -> dict[str, dict[str, Any]]:
        return normalize_scope(self.data.get("overrides", {}).get(umo))

    def set_override(
        self,
        umo: str,
        delta: dict[str, Any],
        *,
        replace: bool = False,
        categories: list[str] | None = None,
    ) -> None:
        overrides = self.data.setdefault("overrides", {})
        overrides[umo] = self._apply_delta(
            normalize_scope(overrides.get(umo)), delta, replace, categories
        )
        self.save()

    def clear_override(self, umo: str, categories: list[str] | None = None) -> None:
        """清除覆盖（恢复继承）。categories 为 None 时删除该 UMO 全部覆盖。"""
        overrides = self.data.setdefault("overrides", {})
        if not categories:
            overrides.pop(umo, None)
            self.save()
            return
        if umo not in overrides:
            return
        scope = normalize_scope(overrides[umo])
        for cat in categories:
            scope[cat] = {}
        if any(scope[cat] for cat in scope):
            overrides[umo] = scope
        else:
            overrides.pop(umo, None)
        self.save()

    # ------------------------------------------------------------------
    # 物化记录与漂移 pin
    # ------------------------------------------------------------------
    def materialized(self, umo: str) -> dict[str, Any]:
        return dict(self.data.get("materialized", {}).get(umo) or {})

    def record_materialized(self, umo: str, key: str, value: Any) -> None:
        mat = self.data.setdefault("materialized", {}).setdefault(umo, {})
        mat[key] = value
        self.save()

    def clear_materialized(self, umo: str, key: str | None = None) -> None:
        mat = self.data.setdefault("materialized", {})
        if key is None:
            mat.pop(umo, None)
        elif umo in mat:
            mat[umo].pop(key, None)
        self.save()

    def pins(self, umo: str) -> list[str]:
        return list(self.data.get("pins", {}).get(umo) or [])

    def is_pinned(self, umo: str, key: str) -> bool:
        return key in self.pins(umo)

    def add_pin(self, umo: str, key: str) -> None:
        pins = self.data.setdefault("pins", {})
        lst = pins.setdefault(umo, [])
        if key not in lst:
            lst.append(key)
            self.save()

    def remove_pin(self, umo: str, key: str | None = None) -> None:
        pins = self.data.setdefault("pins", {})
        if key is None:
            pins.pop(umo, None)
        elif umo in pins:
            pins[umo] = [k for k in pins[umo] if k != key]
            if not pins[umo]:
                pins.pop(umo)
        self.save()

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self, desc: str) -> Path:
        """批量操作前的全量快照（store + registry），返回快照文件路径。"""
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in desc)[:40]
        path = self.snapshot_dir / f"{stamp}-{safe}.json"
        self._write_json_atomic(
            path, {"store": self.data, "registry": self.registry}
        )
        return path

    def list_snapshots(self) -> list[str]:
        if not self.snapshot_dir.exists():
            return []
        return sorted(p.name for p in self.snapshot_dir.glob("*.json"))

    def restore_snapshot(self, name: str) -> None:
        """回滚到指定快照。"""
        path = self.snapshot_dir / name
        if not path.exists():
            raise StoreError(f"快照 '{name}' 不存在")
        payload = self._read_json(path)
        self.data = {**self._default_data(), **(payload.get("store") or {})}
        self.registry = {"sessions": (payload.get("registry") or {}).get("sessions", {})}
        self.save()
        self.save_registry()

    # ------------------------------------------------------------------
    # 内部：差量应用
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_delta(
        base: dict[str, dict[str, Any]],
        delta: dict[str, Any],
        replace: bool,
        categories: list[str] | None,
    ) -> dict[str, dict[str, Any]]:
        """把 delta 应用到 base。

        replace=False（合并式）：逐字段覆盖 delta 中出现的字段；
        replace=True（覆盖式）：被选中的类别整体替换为 delta 中的该类内容。
        categories 非空时只作用于这些类别。
        """
        delta_n = normalize_scope(delta)
        cats = categories or list(base.keys())
        out = normalize_scope(base)
        for cat in cats:
            if cat not in out:
                continue
            if replace:
                out[cat] = dict(delta_n.get(cat, {}))
            elif delta_n.get(cat):
                out[cat] = {**out[cat], **delta_n[cat]}
        return out
