"""群聊指令业务逻辑（与 Star 类解耦，便于单测）。

main.py 的指令 handler 只做三件事：解析 token → 调用 CommandService → 回复文本。

指令一览（前缀 /groupcfg 或 /分组配置）：
  help                              帮助
  status                            概览（会话/分组/整群/快照数）
  me | 生效                         当前会话的生效链（四类字段来自哪一层）
  group list                        分组列表
  group create <名> [优先级]        建组（默认优先级 100）
  group join <组>                   当前会话加入组（同优先级冲突会拒绝）
  group leave <组>                  当前会话移出组
  group addrule <组> <模式>         加通配规则，如 default:GroupMessage:*_123456
  group rmrule <组> <模式>
  group scope <组> show             查看组层配置
  group scope <组> <设置>           设置：persona <id> / model <id> / off <插件> / on <插件>
  global show | set <设置> | reset [类]        全局默认（作用于所有会话）
  cluster show | set <设置> | reset [类]        整群设定（作用于当前会话所属物理群）
  sub show | set <设置> | reset [类]            子会话覆盖（当前会话）
  batch group <组> <设置>           预览"组内批量"
  batch outside <组> <设置>         预览"组外（补集）批量"
  batch show | confirm | cancel     预览 / 确认执行（自动快照） / 取消
  sync                              全量同步（对账物化层）

设置（<设置>）共六种写法：
  persona <人格名> [src]   model <提供商ID>   off/on <插件名>
  src on|off               人格源开关（勾选后本层所选人格=决定性人格，
                           普通聊天与主动消息/动态生活状态等插件生成都用它）
"""

from __future__ import annotations

import time
from typing import Any

from ..core.apply import SyncEngine
from ..core.grouping import (
    PriorityConflictError,
    complement,
    group_matches,
    matching_groups,
)
from ..core.models import (
    CATEGORY_PERSONA,
    CATEGORY_PLUGINS,
    CATEGORY_PROVIDER,
    CATEGORY_SERVICE,
    CATEGORIES,
    cluster_key,
    cluster_rule_patterns,
)
from ..core.resolver import resolve
from ..core.store import PluginStore, StoreError

LAYER_LABEL = {
    "L0": "内置默认",
    "L1": "全局默认",
    "L2": "分组设定",
    "L3": "整群设定",
    "L4": "子会话覆盖",
}

SET_KINDS = ("persona", "model", "off", "on", "src")
SET_KIND_ALIASES = {
    "persona": "persona", "人格": "persona",
    "model": "model", "模型": "model",
    "off": "off", "关": "off", "关插件": "off",
    "on": "on", "开": "on", "开插件": "on",
    "src": "src", "source": "src", "人格源": "src", "源头": "src",
}
SRC_ON = {"on", "1", "true", "yes", "开", "开人格源"}
SRC_OFF = {"off", "0", "false", "no", "关", "关人格源"}


class CommandService:
    """指令分发与业务实现。所有方法返回给人看的文本。"""

    def __init__(self, store: PluginStore, engine: SyncEngine) -> None:
        self.store = store
        self.engine = engine
        self._pending: dict[str, Any] | None = None  # 待确认的批量操作

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------
    async def dispatch(self, tokens: list[str], umo: str) -> str:
        if not tokens or tokens[0] in ("help", "帮助", "?"):
            return self.help()
        head = tokens[0].lower()
        rest = tokens[1:]
        try:
            if head in ("status", "状态"):
                return self.status()
            if head in ("me", "生效", "whoami"):
                return await self.explain(umo)
            if head in ("group", "分组"):
                return await self._group(rest, umo)
            if head in ("global", "全局"):
                return await self._global(rest)
            if head in ("cluster", "整群"):
                return await self._cluster(rest, umo)
            if head in ("sub", "子会话"):
                return await self._sub(rest, umo)
            if head in ("batch", "批量"):
                return await self._batch(rest, umo)
            if head in ("sync", "同步"):
                return await self._sync()
            if head in ("resetall", "恢复全部", "全部恢复"):
                return await self._reset_all()
        except (StoreError, PriorityConflictError, ValueError) as exc:
            return f"操作失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 指令层兜底
            return f"执行出错（{type(exc).__name__}）：{exc}"
        return self.help()

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------
    def help(self) -> str:
        return (
            "【分组配置管家】指令\n"
            "/分组配置 —— 概览    /分组配置 me —— 生效链\n"
            "分组：group list | group create 名 [优先级] | group join/leave 组\n"
            "      group addrule/rmrule 组 模式 | group scope 组 show\n"
            "全局：global show | global set 人格 <名> | global set src on/off\n"
            "整群：cluster show | cluster set 人格 <名> | cluster set 模型 <ID>\n"
            "      cluster set off/on <插件> | cluster reset\n"
            "子会话：sub show | sub set 人格 <名> | sub set 人格源 on | sub reset\n"
            "批量：batch group 组 <设置>（组内）/ batch outside 组 <设置>（组外补集）\n"
            "      batch show | batch confirm | batch cancel\n"
            "其他：sync（全量同步）| resetall（所有会话恢复自动管理）\n"

            "设置写法：persona <人格名> / src on|off（人格源=决定性人格，"
            "主动消息·动态生活状态等插件生成也用它）/ model <提供商ID> / off|on <插件>"
        )

    def status(self) -> str:
        data = self.store.data
        groups = data.get("groups") or {}
        umos = self.store.all_umos()
        return (
            f"会话全集 {len(umos)} 个 | 分组 {len(groups)} 个 | "
            f"整群设定 {len(data.get('clusters') or {})} 个 | "
            f"子会话覆盖 {len(data.get('overrides') or {})} 个 | "
            f"快照 {len(self.store.list_snapshots())} 份"
        )

    async def explain(self, umo: str) -> str:
        res = resolve(self.store.data, umo)
        ck = cluster_key(umo)
        lines = [f"会话：{umo}"]
        lines.append(f"物理群：{ck or '（非群会话）'}")
        if res.matched_groups:
            names = []
            for gid in res.matched_groups:
                g = self.store.data.get("groups", {}).get(gid, {})
                names.append(f"{g.get('name', gid)}(P{g.get('priority', '?')})")
            lines.append("命中分组（低→高）：" + " → ".join(names))
        else:
            lines.append("命中分组：无")
        pins = self.store.pins(umo)
        if pins:
            lines.append("手动覆盖(pin)：" + ",".join(pins))
        for cat in CATEGORIES:
            cat_vals = res.values.get(cat) or {}
            if not cat_vals:
                lines.append(f"[{cat}] （继承中，本会话未设置）")
                continue
            lines.append(f"[{cat}]")
            for fname, val in cat_vals.items():
                src = (res.sources.get(cat) or {}).get(fname, "?")
                layer, _, detail = src.partition(":")
                label = LAYER_LABEL.get(layer, layer)
                where = f"（{detail}）" if detail and layer != "L1" else ""
                lines.append(f"  {fname} = {val}  ← {label}{where}")
        # 人格源（决定性人格）：勾选层所选人格 = 一切数据的源头
        psrc = res.value(CATEGORY_PERSONA, "persona_source")
        if psrc is True:
            pid = res.value(CATEGORY_PERSONA, "persona_id")
            if pid:
                src = res.source(CATEGORY_PERSONA, "persona_id") or "?"
                layer, _, detail = src.partition(":")
                label = LAYER_LABEL.get(layer, layer)
                where = f"（{detail}）" if detail and layer != "L1" else ""
                lines.append(
                    f"★决定性人格（人格源开启）：{pid}  ← {label}{where}"
                    "（普通聊天与主动消息/动态生活状态等插件生成都用它）"
                )
            else:
                lines.append("⚠ 人格源已勾选但本会话没有解析到人格 → 暂无决定性人格")
        else:
            lines.append("人格源：未开启（本会话人格不作为源头）")
        # 原生层当前实际生效值
        for key in (
            "session_service_config",
            "provider_perf_chat_completion",
            "session_plugin_config",
        ):
            current = await self.engine.hooks.session_get(umo, key, None)
            if current is not None:
                shown = current if not isinstance(current, dict) else current
                lines.append(f"原生[{key}] = {shown}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 分组
    # ------------------------------------------------------------------
    async def _group(self, tokens: list[str], umo: str) -> str:
        if not tokens or tokens[0] in ("list", "列表"):
            groups = sorted(
                self.store.groups().values(),
                key=lambda g: (g.priority, g.created_at),
            )
            if not groups:
                return "还没有分组。用 group create a 创建一个。"
            lines = ["分组列表（优先级低→高）："]
            for g in groups:
                scope_bits = []
                if g.scope.get(CATEGORY_PERSONA):
                    scope_bits.append(f"人格={g.scope[CATEGORY_PERSONA].get('persona_id')}")
                if g.scope.get(CATEGORY_PROVIDER):
                    scope_bits.append(f"模型={g.scope[CATEGORY_PROVIDER].get('provider_id')}")
                if g.scope.get(CATEGORY_PLUGINS):
                    scope_bits.append(f"插件开关={g.scope[CATEGORY_PLUGINS]}")
                mark = " ◀当前会话在此" if group_matches(g, umo) else ""
                lines.append(
                    f"  [{g.id}] {g.name} (P{g.priority}) 成员{len(g.members)} "
                    f"规则{len(g.rules)} {' '.join(scope_bits)}{mark}"
                )
            return "\n".join(lines)

        sub, rest = tokens[0].lower(), tokens[1:]
        if sub in ("create", "建", "新建"):
            if not rest:
                return "用法：group create <名> [优先级]"
            name = rest[0]
            priority = int(rest[1]) if len(rest) > 1 else 100
            g = self.store.create_group(name, priority=priority)
            return f"已创建分组「{g.name}」[{g.id}]，优先级 {g.priority}。"

        if sub in ("join", "加入"):
            g = self._find_group(rest)
            self.store.add_member(g.id, umo)
            await self.engine.sync_session(umo, register=False)
            return f"当前会话已加入「{g.name}」(P{g.priority})，配置已同步。"

        if sub in ("leave", "移出", "退出"):
            g = self._find_group(rest)
            self.store.remove_member(g.id, umo)
            await self.engine.sync_session(umo, register=False)
            return f"当前会话已移出「{g.name}」，配置已同步。"

        if sub in ("addrule", "规则加"):
            g = self._find_group(rest[:1])
            if len(rest) < 2:
                return "用法：group addrule <组> <模式>，如 default:GroupMessage:*_123456"
            rule = rest[1]
            self.store.add_rule(g.id, rule)
            targets = self._umos_hit_by_group(g.id)
            await self.engine.sync_many(targets)
            return f"「{g.name}」新增规则 {rule}，已同步 {len(targets)} 个已知会话。"

        if sub in ("rmrule", "规则减"):
            g = self._find_group(rest[:1])
            if len(rest) < 2:
                return "用法：group rmrule <组> <模式>"
            self.store.remove_rule(g.id, rest[1])
            await self.engine.sync_session(umo, register=False)
            return f"「{g.name}」已移除规则 {rest[1]}。"

        if sub in ("scope", "配置"):
            return await self._group_scope(rest, umo)
        if sub == "joincluster":
            # 便捷：把某个物理群整群加入本组
            if len(rest) < 2:
                return "用法：group joincluster <组> <平台> <群号>"
            g = self._find_group(rest[:1])
            platform, gid = rest[1], rest[2] if len(rest) > 2 else ""
            if not gid:
                return "用法：group joincluster <组> <平台> <群号>"
            for pat in cluster_rule_patterns(platform, gid):
                self.store.add_rule(g.id, pat)
            targets = self._umos_hit_by_group(g.id)
            await self.engine.sync_many(targets)
            return (
                f"「{g.name}」已整群加入 {platform} 群 {gid}（2 条规则），"
                f"同步 {len(targets)} 个已知会话。"
            )
        return self.help()

    async def _group_scope(self, tokens: list[str], umo: str) -> str:
        if len(tokens) < 2:
            return "用法：group scope <组> show | group scope <组> <设置>"
        g = self._find_group([tokens[0]])
        rest = tokens[1:]
        if rest[0] in ("show", "查看"):
            return f"「{g.name}」组层配置：{g.scope if any(g.scope.values()) else '（空，全继承）'}"
        delta, desc = self._make_delta(rest, g.scope)
        self.store.set_group_scope(g.id, delta)
        targets = self._umos_hit_by_group(g.id)
        reports = await self.engine.sync_many(targets)
        return (
            f"「{g.name}」组层已设置 {desc}，"
            f"已同步 {len(targets)} 个会话（写入 {sum(len(r.wrote) for r in reports)} 处）。"
        )

    # ------------------------------------------------------------------
    # 全局（L1）
    # ------------------------------------------------------------------
    async def _global(self, tokens: list[str]) -> str:
        if not tokens or tokens[0] in ("show", "查看"):
            scope = self.store.global_scope()
            return f"全局层配置：{scope if any(scope.values()) else '（空，全继承）'}"
        sub = tokens[0].lower()
        rest = tokens[1:]
        if sub in ("set", "设置", "设"):
            delta, desc = self._make_delta(rest, self.store.global_scope())
            self.store.set_global_scope(delta)
            targets = list(self.store.all_umos())
            reports = await self.engine.sync_many(targets)
            return (
                f"全局层已设置 {desc}，"
                f"已同步 {len(targets)} 个会话（写入 {sum(len(r.wrote) for r in reports)} 处）。"
            )
        if sub in ("reset", "重置", "清除"):
            cat = self._reset_category(rest)
            self.store.set_global_scope(
                {c: {} for c in (CATEGORIES if not cat else (cat,))},
                replace=True,
                categories=None if not cat else [cat],
            )
            targets = list(self.store.all_umos())
            await self.engine.sync_many(targets)
            return (
                f"全局层已清除 {'全部' if not cat else cat} 配置，"
                f"{len(targets)} 个会话重新继承。"
            )
        return self.help()

    # ------------------------------------------------------------------
    # 整群
    # ------------------------------------------------------------------
    async def _cluster(self, tokens: list[str], umo: str) -> str:
        ck = cluster_key(umo)
        if not ck:
            return "当前会话不是群会话，无法使用整群设定。"
        if not tokens or tokens[0] in ("show", "查看"):
            scope = self.store.cluster_scope(ck)
            return f"整群 {ck} 配置：{scope if any(scope.values()) else '（空，全继承）'}"
        sub = tokens[0].lower()
        rest = tokens[1:]
        if sub in ("set", "设置", "设"):
            delta, desc = self._make_delta(rest, self.store.cluster_scope(ck))
            self.store.set_cluster_scope(ck, delta)
            targets = self._umos_in_cluster(ck)
            reports = await self.engine.sync_many(targets)
            return (
                f"整群 {ck} 已设置 {desc}，"
                f"已同步 {len(targets)} 个已知子会话（写入 {sum(len(r.wrote) for r in reports)} 处）。"
            )
        if sub in ("reset", "重置", "清除"):
            cat = self._reset_category(rest)
            self.store.set_cluster_scope(
                ck,
                {c: {} for c in (CATEGORIES if not cat else (cat,))},
                replace=True,
                categories=None if not cat else [cat],
            )
            targets = self._umos_in_cluster(ck)
            await self.engine.sync_many(targets)
            return f"整群 {ck} 已清除 {'全部' if not cat else cat} 配置，{len(targets)} 个会话重新继承。"
        if sub in ("addrule",):  # 兼容误输
            return "整群设定对本群自动生效，无需加规则。"
        return self.help()

    # ------------------------------------------------------------------
    # 子会话
    # ------------------------------------------------------------------
    async def _sub(self, tokens: list[str], umo: str) -> str:
        if not tokens or tokens[0] in ("show", "查看"):
            return await self.explain(umo)
        sub = tokens[0].lower()
        rest = tokens[1:]
        if sub in ("set", "设置", "设"):
            delta, desc = self._make_delta(rest, self.store.override(umo))
            self.store.set_override(umo, delta)
            rep = await self.engine.sync_session(umo, register=False)
            return f"当前子会话已覆盖 {desc}，同步：{rep.summary()}"
        if sub in ("reset", "重置", "恢复"):
            cat = self._reset_category(rest)
            self.store.clear_override(umo, None if not cat else [cat])
            rep = await self.engine.sync_session(umo, register=False)
            return f"当前子会话已{'清除全部覆盖' if not cat else f'清除 {cat} 覆盖'}，恢复继承。{rep.summary()}"
        if sub in ("takeover", "接管"):
            self.engine.takeover(umo)
            self.store.clear_override(umo)
            self.store.clear_materialized(umo)
            rep = await self.engine.sync_session(umo, register=False)
            return f"已重新交给分组管（清掉手动改过标记与单独设置）。{rep.summary()}"
        return self.help()

    # ------------------------------------------------------------------
    # 批量（组内 / 补集）
    # ------------------------------------------------------------------
    async def _batch(self, tokens: list[str], umo: str) -> str:
        if not tokens:
            return self.help()
        sub = tokens[0].lower()
        rest = tokens[1:]
        if sub in ("show", "预览"):
            if not self._pending:
                return "没有待确认的批量操作。先用 batch group/batch outside 预览。"
            p = self._pending
            preview = "、".join(p["umos"][:8])
            more = f" 等 {len(p['umos'])} 个" if len(p["umos"]) > 8 else ""
            return f"待确认：{p['desc']} → {p['desc_kind']} {p['desc_value']}\n目标：{preview}{more}\n回复 /分组配置 batch confirm 执行（会自动快照），或 batch cancel 取消。"
        if sub in ("cancel", "取消"):
            self._pending = None
            return "已取消。"
        if sub in ("confirm", "确认", "yes"):
            if not self._pending:
                return "没有待确认的批量操作。"
            p = self._pending
            self._pending = None
            snap = self.store.snapshot(f"batch-{p['desc_kind']}")
            for target in p["umos"]:
                self.store.set_override(target, p["delta"])
            reports = await self.engine.sync_many(p["umos"])
            wrote = sum(len(r.wrote) for r in reports)
            removed = sum(len(r.removed) for r in reports)
            return (
                f"批量完成：{p['desc']}，目标 {len(p['umos'])} 个会话，"
                f"写入 {wrote} 处、清除 {removed} 处。\n"
                f"快照：{snap.name}（可用 WebUI 或后续指令回滚）"
            )
        if sub in ("group", "组内", "in"):
            if len(rest) < 2:
                return "用法：batch group <组> <设置>"
            g = self._find_group([rest[0]])
            targets = self._umos_hit_by_group(g.id)
            return await self._stage_batch(f"组内[{g.name}]", rest[1:], targets, g.scope)
        if sub in ("outside", "组外", "补集", "except"):
            if len(rest) < 2:
                return "用法：batch outside <组> <设置>（对组外全部会话生效）"
            g = self._find_group([rest[0]])
            groups = self.store.groups()
            targets = complement(self.store.all_umos(), [groups[g.id]])
            return await self._stage_batch(f"补集[{g.name}外]", rest[1:], targets, {})
        return self.help()

    async def _stage_batch(
        self,
        desc: str,
        set_tokens: list[str],
        targets: list[str],
        current_scope: dict[str, Any],
    ) -> str:
        if not targets:
            return f"{desc}：目标集合为空，没有可应用的会话（先用 sync 或让会话出现）。"
        delta, value_desc = self._make_delta(set_tokens, current_scope)
        self._pending = {
            "desc": desc,
            "umos": targets,
            "delta": delta,
            "desc_kind": set_tokens[0],
            "desc_value": " ".join(set_tokens[1:]),
        }
        preview = "、".join(targets[:8])
        more = f"…共 {len(targets)} 个" if len(targets) > 8 else ""
        return (
            f"【预览】{desc} → {value_desc}\n"
            f"将影响 {len(targets)} 个会话：{preview}{more}\n"
            f"确认请回复 /分组配置 batch confirm（自动快照可回滚），取消回复 batch cancel。"
        )

    async def _reset_all(self) -> str:
        """清空所有子会话单独设置与手动改过标记（takeover 式：写入记录一并清，
        否则重新对账时旧记录一比对，标记立刻被打回来），再按分组/群聊/全局下发。"""
        self.store.data["pins"] = {}
        self.store.data["overrides"] = {}
        for umo in self.store.all_umos():
            self.store.clear_materialized(umo)
        self.store.save()
        reports = await self.engine.reconcile_all()
        wrote = sum(len(r.wrote) for r in reports)
        return (
            f"已把全部会话恢复为「自动管理」：清空了单独设置和手动改过标记，"
            f"重新同步 {len(reports)} 个会话（写入 {wrote} 处）。"
            f"分组 / 群聊 / 全局的设置保持不变。"
        )

    async def _sync(self) -> str:
        reports = await self.engine.reconcile_all()
        wrote = sum(len(r.wrote) for r in reports)
        drift = sum(len(r.drift) for r in reports)
        pinned = sum(len(r.pinned_skipped) for r in reports)
        errors = [r for r in reports if r.error]
        msg = (
            f"全量同步完成：{len(reports)} 个会话，写入 {wrote} 处，"
            f"漂移转手动覆盖 {drift} 处，pin 跳过 {pinned} 处"
        )
        if errors:
            msg += f"，失败 {len(errors)} 个（首例：{errors[0].error}）"
        return msg

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _find_group(self, tokens: list[str]):
        if not tokens:
            raise StoreError("缺少分组名/ID")
        q = tokens[0]
        groups = self.store.groups()
        if q in groups:
            return groups[q]
        for g in groups.values():
            if g.name == q:
                return g
        raise StoreError(f"找不到分组 '{q}'（可用：{'、'.join(x.name for x in groups.values()) or '无'}）")

    @staticmethod
    def _reset_category(tokens: list[str]) -> str | None:
        if not tokens:
            return None
        cat = tokens[0].lower()
        mapping = {
            "persona": CATEGORY_PERSONA, "人格": CATEGORY_PERSONA,
            "model": CATEGORY_PROVIDER, "模型": CATEGORY_PROVIDER,
            "service": CATEGORY_SERVICE, "服务": CATEGORY_SERVICE,
            "plugins": CATEGORY_PLUGINS, "插件": CATEGORY_PLUGINS,
        }
        if cat not in mapping:
            raise ValueError(f"未知类别 '{cat}'，可用：persona/model/service/plugins")
        return mapping[cat]

    def _make_delta(
        self,
        set_tokens: list[str],
        current_scope: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """把 persona/model/off/on 设置 token 翻译成四类 scope 差量。"""
        if len(set_tokens) < 2:
            raise ValueError(
                "设置格式：persona <人格名> / model <提供商ID> / off <插件> / on <插件>"
            )
        raw_kind = set_tokens[0].lower()
        kind = SET_KIND_ALIASES.get(raw_kind)
        if kind not in SET_KINDS:
            raise ValueError(f"未知设置类型 '{set_tokens[0]}'，可用：persona/model/off/on")
        value = set_tokens[1]
        if kind == "persona":
            delta: dict[str, Any] = {CATEGORY_PERSONA: {"persona_id": value}}
            desc = f"人格={value}"
            # 尾挂写法：persona <名> src —— 设人格的同时打开本层人格源
            if len(set_tokens) > 2 and set_tokens[2].lower() in (
                "src", "source", "人格源", "源头",
            ):
                delta[CATEGORY_PERSONA]["persona_source"] = True
                desc += "，人格源=开"
            return delta, desc
        if kind == "src":
            flag_raw = value.lower()
            if flag_raw in SRC_ON:
                flag = True
            elif flag_raw in SRC_OFF:
                flag = False
            else:
                raise ValueError("人格源开关写法：src on | src off")
            return (
                {CATEGORY_PERSONA: {"persona_source": flag}},
                f"人格源={'开（本层所选人格=决定性人格，其他插件生成也用它）' if flag else '关'}",
            )
        if kind == "model":
            return {CATEGORY_PROVIDER: {"provider_id": value}}, f"模型={value}"
        # 插件开关：读该层现有列表做增删（列表字段是整体替换语义）
        cur = current_scope.get(CATEGORY_PLUGINS, {}) or {}
        enabled = list(cur.get("enabled_plugins") or [])
        disabled = list(cur.get("disabled_plugins") or [])
        if kind == "off":
            if value not in disabled:
                disabled.append(value)
            if value in enabled:
                enabled.remove(value)
            desc = f"关闭插件 {value}"
        else:
            if value not in enabled:
                enabled.append(value)
            if value in disabled:
                disabled.remove(value)
            desc = f"开启插件 {value}"
        return (
            {CATEGORY_PLUGINS: {"enabled_plugins": enabled, "disabled_plugins": disabled}},
            desc,
        )

    def _umos_hit_by_group(self, gid: str) -> list[str]:
        groups = self.store.groups()
        if gid not in groups:
            return []
        g = groups[gid]
        # 规则可能命中的未知新会话由事件侧接入时补齐，这里只处理登记簿内
        return sorted({u for u in self.store.all_umos() if group_matches(g, u)})

    def _umos_in_cluster(self, ck: str) -> list[str]:
        return [u for u in self.store.all_umos() if cluster_key(u) == ck]
