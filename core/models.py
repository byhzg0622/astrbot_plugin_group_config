"""数据结构与 UMO 解析工具（纯 Python，不依赖 AstrBot 运行时）。

对应 docs/DESIGN.md 第 1、2 节：UMO 解析、作用域（Scope）、五层继承的载体。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 四类配置（作用域内的类别）
# ---------------------------------------------------------------------------
CATEGORY_PERSONA = "persona"
CATEGORY_PROVIDER = "provider"
CATEGORY_SERVICE = "service"
CATEGORY_PLUGINS = "plugins"
CATEGORIES: tuple[str, ...] = (
    CATEGORY_PERSONA,
    CATEGORY_PROVIDER,
    CATEGORY_SERVICE,
    CATEGORY_PLUGINS,
)

# 每类允许存在的字段（用于 normalize 校验）
CATEGORY_FIELDS: dict[str, tuple[str, ...]] = {
    # persona_source（人格源/决定性人格）：bool，随五层继承；开=本层所选人格是
    # 一切数据的源头（普通聊天与其他插件生成时都用它），见 docs/DESIGN.md 第 11 节。
    CATEGORY_PERSONA: ("persona_id", "persona_source"),
    CATEGORY_PROVIDER: ("provider_id",),
    CATEGORY_SERVICE: ("llm_enabled", "tts_enabled", "session_enabled"),
    CATEGORY_PLUGINS: ("enabled_plugins", "disabled_plugins"),
}

# ---------------------------------------------------------------------------
# 五层标识
# ---------------------------------------------------------------------------
LAYER_DEFAULT = "L0"  # 内置默认（空 scope，不物化，保留 AstrBot 原生状态）
LAYER_GLOBAL = "L1"
LAYER_GROUP = "L2"
LAYER_CLUSTER = "L3"
LAYER_SESSION = "L4"

UNGROUPED_ID = "__ungrouped__"
""""未分类"层在来源标注中的组 id（未命中任何分组的会话使用）。"""

# ---------------------------------------------------------------------------
# 物化目标：AstrBot 原生 sp key（与官方"自定义规则"同构）
# ---------------------------------------------------------------------------
KEY_SESSION_SERVICE = "session_service_config"
KEY_SESSION_PLUGIN = "session_plugin_config"
KEY_PROVIDER_CHAT = "provider_perf_chat_completion"

# 类别 -> 物化 key。注意 persona 与 service 共用一个 key（官方如此存储）。
CATEGORY_KEYS: dict[str, str] = {
    CATEGORY_PERSONA: KEY_SESSION_SERVICE,
    CATEGORY_SERVICE: KEY_SESSION_SERVICE,
    CATEGORY_PROVIDER: KEY_PROVIDER_CHAT,
    CATEGORY_PLUGINS: KEY_SESSION_PLUGIN,
}


@dataclass(frozen=True)
class UmoParts:
    """UMO 三段式解析结果：[platform]:[message_type]:[session_id]。"""

    platform: str
    message_type: str
    session_id: str


def parse_umo(umo: str) -> UmoParts | None:
    """按核心 UmopConfigRouter 同款语义拆 UMO（split(":", 2)，session_id 允许含 ":"）。

    Returns:
        UmoParts 或 None（格式非法）。
    """
    if not isinstance(umo, str):
        return None
    parts = umo.split(":", 2)
    if len(parts) != 3:
        return None
    return UmoParts(parts[0], parts[1], parts[2])


def cluster_key(umo: str) -> str | None:
    """从 UMO 提取物理群 key，无法归类为群会话时返回 None。

    session_id 形态（用户确认的实测格式）：
    - 群内子会话："本人QQ号_群号"，**群号在 "_" 之后**；
    - 裸群会话：整段即群号；
    - 私聊（FriendMessage）与 webchat：不提取，返回 None。

    message_type 归一为 "group"，使 GroupMessage/Group 等变体与
    裸群 UMO、子会话 UMO 都落到同一个 cluster key。
    """
    parts = parse_umo(umo)
    if parts is None:
        return None
    if "group" not in parts.message_type.lower():
        return None
    session_id = parts.session_id
    group_id = session_id.split("_", 1)[1] if "_" in session_id else session_id
    group_id = group_id.strip()
    if not group_id:
        return None
    return f"{parts.platform}::group::{group_id}"


def is_group_sub_session(umo: str) -> bool:
    """是否为"单独记忆"隔离出的子会话（群 UMO 且 session_id 带 _群号 后缀）。"""
    parts = parse_umo(umo)
    if parts is None or cluster_key(umo) is None:
        return False
    return "_" in parts.session_id


def sub_session_user(umo: str) -> str | None:
    """子会话中的发送者 QQ 号（session_id "_" 前段）；裸群会话/私聊返回 None。"""
    parts = parse_umo(umo)
    if parts is None or not is_group_sub_session(umo):
        return None
    return parts.session_id.split("_", 1)[0]


def cluster_rule_patterns(
    platform: str,
    group_id: str,
    message_type: str = "*",
) -> list[str]:
    """生成匹配某物理群**全部会话**的两条 fnmatch 规则（裸群会话 + 群内子会话）。

    例：cluster_rule_patterns("default", "1053676331")
        -> ["default:*:1053676331", "default:*:*_1053676331"]
    因为子会话是"本人QQ号_群号"、裸群会话整段是群号，一条通配盖不住两者。
    """
    return [
        f"{platform}:{message_type}:{group_id}",
        f"{platform}:{message_type}:*_{group_id}",
    ]


# ---------------------------------------------------------------------------
# Scope（作用域）操作
# ---------------------------------------------------------------------------
def empty_scope() -> dict[str, dict[str, Any]]:
    """空作用域：四类各一个空 dict（表示"本层什么都没设"）。"""
    return {cat: {} for cat in CATEGORIES}


def normalize_scope(raw: Any) -> dict[str, dict[str, Any]]:
    """把任意输入规整为合法 Scope；非法类别/字段被丢弃，不抛异常。

    字段类型做基本校验：bool 字段须为 bool，list 字段须为 list[str]，
    persona_id/provider_id 须为非空 str。
    """
    scope = empty_scope()
    if not isinstance(raw, dict):
        return scope
    for cat in CATEGORIES:
        cat_raw = raw.get(cat)
        if not isinstance(cat_raw, dict):
            continue
        cleaned: dict[str, Any] = {}
        for f in CATEGORY_FIELDS[cat]:
            if f not in cat_raw:
                continue
            v = cat_raw[f]
            if f in ("persona_id", "provider_id"):
                if isinstance(v, str) and v:
                    cleaned[f] = v
            elif f in ("enabled_plugins", "disabled_plugins"):
                if isinstance(v, list):
                    cleaned[f] = [str(x) for x in v if str(x)]
            else:  # service 的三个 bool
                if isinstance(v, bool):
                    cleaned[f] = v
        if cleaned:
            scope[cat] = cleaned
    return scope


def merge_scope(
    base: dict[str, dict[str, Any]],
    overlay: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """字段级差量合并：overlay 中出现的字段覆盖 base，未出现的保留。"""
    merged = empty_scope()
    for cat in CATEGORIES:
        merged[cat] = {**base.get(cat, {}), **overlay.get(cat, {})}
    return merged


def scope_is_empty(scope: dict[str, dict[str, Any]]) -> bool:
    """该层是否完全没有设置任何字段。"""
    return not any(scope.get(cat) for cat in CATEGORIES)


def prune_scope(
    scope: dict[str, dict[str, Any]],
    categories: list[str] | tuple[str, ...] | None,
) -> dict[str, dict[str, Any]]:
    """只保留指定类别（categories 为 None 时全保留）。批量选择性应用用。"""
    out = empty_scope()
    if not categories:
        return scope
    for cat in categories:
        if cat in CATEGORIES:
            out[cat] = dict(scope.get(cat, {}))
    return out


# ---------------------------------------------------------------------------
# 分组数据
# ---------------------------------------------------------------------------
@dataclass
class Group:
    """一个分组（a/b/c…）。同 priority 下一个 UMO 只能属于一个分组。"""

    id: str
    name: str
    priority: int = 100
    members: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    scope: dict[str, dict[str, Any]] = field(default_factory=empty_scope)
    created_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "priority": self.priority,
            "members": list(self.members),
            "rules": list(self.rules),
            "scope": normalize_scope(self.scope),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, gid: str, data: dict[str, Any]) -> "Group":
        return cls(
            id=gid,
            name=str(data.get("name", gid)),
            priority=int(data.get("priority", 100)),
            members=[str(m) for m in data.get("members", [])],
            rules=[str(r) for r in data.get("rules", [])],
            scope=normalize_scope(data.get("scope")),
            created_at=int(data.get("created_at", 0)),
        )


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------
@dataclass
class ResolvedConfig:
    """resolve(umo) 的输出：值 + 字段级来源层标注。

    values: {category: {field: value}}
    sources: {category: {field: "L2:group:a" 形式的来源标注}}
    matched_groups: 按 (priority 升序, created_at) 排序的命中分组 id
    """

    values: dict[str, dict[str, Any]] = field(default_factory=empty_scope)
    sources: dict[str, dict[str, str]] = field(default_factory=dict)
    matched_groups: list[str] = field(default_factory=list)

    def value(self, category: str, field_name: str, default: Any = None) -> Any:
        return self.values.get(category, {}).get(field_name, default)

    def source(self, category: str, field_name: str) -> str | None:
        return self.sources.get(category, {}).get(field_name)
