"""五层继承解析（纯 Python）。

对应 docs/DESIGN.md 第 2.3 节：L0 内置 < L1 全局 < L2 分组(按 priority 升序合并)
< L3 整群 < L4 子会话覆盖。输出字段级来源标注。
"""

from __future__ import annotations

from typing import Any

from .grouping import matching_groups
from .models import (
    CATEGORIES,
    LAYER_CLUSTER,
    LAYER_GLOBAL,
    LAYER_GROUP,
    LAYER_SESSION,
    UNGROUPED_ID,
    Group,
    ResolvedConfig,
    cluster_key,
    normalize_scope,
)


def resolve(
    data: dict[str, Any],
    umo: str,
    *,
    ck: str | None = None,
) -> ResolvedConfig:
    """对指定 UMO 执行五层解析。

    Args:
        data: PluginStore.data（定义层）。
        umo: 目标会话 UMO。
        ck: 物理群 key；None 时从 UMO 自行推导。
    """
    res = ResolvedConfig()

    def apply(scope: dict[str, dict[str, Any]], src: str) -> None:
        for cat in CATEGORIES:
            cat_scope = scope.get(cat) or {}
            if not cat_scope:
                continue
            res.values.setdefault(cat, {}).update(cat_scope)
            res.sources.setdefault(cat, {}).update(
                {f: src for f in cat_scope}
            )

    # L0 内置默认：空 scope，不设值（保留 AstrBot 原生状态）
    # L1 全局默认
    apply(normalize_scope(data.get("global_scope")), f"{LAYER_GLOBAL}:global")

    # L2 分组设定：按 (priority 升序, created_at) 依次合并，后者覆盖；
    # 未命中任何分组的会话使用"未分类层"（可单独配置，介于全局与分组之间）
    groups = {
        gid: Group.from_dict(gid, raw)
        for gid, raw in (data.get("groups") or {}).items()
    }
    hits = matching_groups(groups, umo)
    if hits:
        for g in hits:
            apply(g.scope, f"{LAYER_GROUP}:group:{g.id}")
    else:
        apply(
            normalize_scope(data.get("ungrouped_scope")),
            f"{LAYER_GROUP}:group:{UNGROUPED_ID}",
        )
    res.matched_groups = [g.id for g in hits]

    # L3 整群设定
    ck = ck or cluster_key(umo)
    if ck:
        cluster = (data.get("clusters") or {}).get(ck)
        if cluster:
            apply(
                normalize_scope(cluster.get("scope")),
                f"{LAYER_CLUSTER}:cluster:{ck}",
            )

    # L4 子会话覆盖（含被 pin 的手改值，其"当前生效值"由 apply 层读 sp 回填）
    override = (data.get("overrides") or {}).get(umo)
    if override:
        apply(normalize_scope(override), f"{LAYER_SESSION}:session")

    return res
