"""分组成员判定与集合运算（纯 Python）。

对应 docs/DESIGN.md 第 3 节：显式成员、fnmatch 规则（与核心 UMOP 同语义）、
全集与补集、同优先级互斥校验。
"""

from __future__ import annotations

import fnmatch

from .models import Group


class PriorityConflictError(ValueError):
    """同一优先级下重复归属：目标 UMO 已在该优先级的其他分组中。"""

    def __init__(self, umo: str, conflict_group: Group) -> None:
        self.umo = umo
        self.conflict_group = conflict_group
        super().__init__(
            f"{umo} 已在优先级 {conflict_group.priority} 的分组 "
            f"'{conflict_group.name}' 中，不能同时属于同一优先级的两个分组"
        )


def rule_matches(rule: str, umo: str) -> bool:
    """规则是否命中 UMO。fnmatch 大小写敏感，与核心 UmopConfigRouter 一致。"""
    return fnmatch.fnmatchcase(umo, rule)


def group_matches(group: Group, umo: str) -> bool:
    """分组是否包含该 UMO（显式成员或规则命中）。"""
    if umo in group.members:
        return True
    return any(rule_matches(rule, umo) for rule in group.rules)


def matching_groups(all_groups: dict[str, Group], umo: str) -> list[Group]:
    """返回包含该 UMO 的所有分组，按 (priority 升序, created_at, id) 排序。

    合并时按此顺序依次应用，高优先级在后、覆盖低优先级。
    """
    hits = [g for g in all_groups.values() if group_matches(g, umo)]
    hits.sort(key=lambda g: (g.priority, g.created_at, g.id))
    return hits


def member_umos(group: Group, all_umos: list[str]) -> list[str]:
    """分组在给定全集内的命中成员（保持全集顺序）。"""
    return [umo for umo in all_umos if group_matches(group, umo)]


def complement(all_umos: list[str], selected_groups: list[Group]) -> list[str]:
    """补集：全集 − 所选分组命中集的并集（"分组外"批量的基准）。"""
    excluded: set[str] = set()
    for g in selected_groups:
        excluded.update(member_umos(g, all_umos))
    return [umo for umo in all_umos if umo not in excluded]


def find_priority_conflict(
    all_groups: dict[str, Group],
    umo: str,
    target: Group,
) -> Group | None:
    """校验同优先级互斥：同 priority 的**其他**分组是否已持有该 UMO（显式或规则命中）。

    target 自身是否已持有该 UMO 不影响判定；target 应先设好新 priority 再调用。
    """
    for other in all_groups.values():
        if other.id == target.id:
            continue
        if other.priority != target.priority:
            continue
        if umo in other.members or any(
            rule_matches(rule, umo) for rule in other.rules
        ):
            return other
    return None
