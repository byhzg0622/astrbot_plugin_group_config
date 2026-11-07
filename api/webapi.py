"""Web API：pages/console 管理页的后端接口（经 bridge 调用，不走 HTTP OpenAPI）。

设计要点（对照 docs/DESIGN.md 第 8.3 节与 ai_mind 示例的经验）：
- bridge 只有 apiGet/apiPost，所以所有路由 GET+POST 双注册；
- 参数三处合并：query、JSON body（web_request.json() 不传 default，老版本会 TypeError）；
- 路由前缀必须含插件名；另挂 <path:rest> 兜底按首段分发；
- 写操作完成后立即对受影响目标做同步（物化）。

挂载方式：main.py 的 GroupConfigPlugin(WebApiMixin, Star)，__init__ 末尾调
register_web_apis()。Mixin 依赖主类先初始化 self.store / self.engine。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger

try:  # astrbot.api.web 在较新版本提供；缺失时降级为直通垫片（页面仍可部分工作）
    from astrbot.api.web import error_response, json_response, request as web_request
except Exception:  # noqa: BLE001
    web_request = None  # type: ignore[assignment]

    def json_response(data: Any = None, **_kwargs: Any) -> Any:  # type: ignore[misc]
        return data if data is not None else {"ok": True}

    def error_response(message: str, **kwargs: Any) -> Any:  # type: ignore[misc]
        return {"error": message, "status_code": kwargs.get("status_code", 400)}


from ..core.apply import (
    PLUGIN_MANAGED_FIELDS,
    SERVICE_MANAGED_FIELDS,
    project_managed,
)
from ..core.grouping import PriorityConflictError, complement, group_matches
from ..core.models import (
    CATEGORIES,
    CATEGORY_PERSONA,
    KEY_PROVIDER_CHAT,
    KEY_SESSION_PLUGIN,
    KEY_SESSION_SERVICE,
    cluster_key,
    is_group_sub_session,
    normalize_scope,
    sub_session_user,
)
from ..core.resolver import resolve
from ..core.store import StoreError

PLUGIN_NAME = "astrbot_plugin_group_config"


def _pick(obj: Any, *keys: str, default: Any = "") -> Any:
    """从 dict 或对象上按候选键取第一个存在的值。"""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] is not None:
                return obj[k]
    for k in keys:
        v = getattr(obj, k, None)
        if v is not None:
            return v
    return default


class WebApiMixin:
    """挂在 Star 类上的面板接口集合。"""

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def _route_defs(self) -> list[tuple[str, Any, str]]:
        return [
            ("overview", self.api_overview, "概览统计"),
            ("sessions", self.api_sessions, "会话全集"),
            ("pools", self.api_pools, "人格池/模型池/插件清单"),
            ("groups", self.api_groups, "分组查询与操作"),
            ("scope", self.api_scope, "四层作用域读写"),
            ("resolve", self.api_resolve, "生效链"),
            ("sync", self.api_sync, "同步/全量对账"),
            ("batch", self.api_batch, "批量预览/提交/回滚"),
            ("snapshots", self.api_snapshots, "快照列表"),
            ("takeover", self.api_takeover, "重新接管"),
            ("reset_all", self.api_reset_all, "全部恢复自动管理"),
            ("factory_reset", self.api_factory_reset, "完全恢复原状"),
            ("clusters", self.api_clusters, "群聊列表与整群设定状态"),
        ]

    def register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[分组配置管家] 当前 AstrBot 无 context.register_web_api，"
                "WebUI 管理页不可用（聊天与指令不受影响）"
            )
            return
        ok = 0
        for name, handler, desc in self._route_defs():
            try:
                register(
                    f"/{PLUGIN_NAME}/{name}",
                    handler,
                    ["GET", "POST"],
                    f"分组配置：{desc}",
                )
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[分组配置管家] 注册面板接口 {name} 失败: {exc}")
        try:
            register(
                f"/{PLUGIN_NAME}/<path:rest>",
                self._api_fallback,
                ["GET", "POST"],
                "分组配置：兜底",
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[分组配置管家] 注册兜底路由失败: {exc}")
        logger.info(f"[分组配置管家] 面板接口已注册 {ok} 个")

    # ------------------------------------------------------------------
    # 请求工具
    # ------------------------------------------------------------------
    async def _params(self) -> dict[str, Any]:
        """合并 query 与 JSON body（三处都读，兼容桥的 GET/POST 两种风格）。"""
        merged: dict[str, Any] = {}
        if web_request is None:
            return merged
        try:
            for key in list(web_request.query.keys()):
                merged[key] = web_request.query.get(key)
        except Exception:  # noqa: BLE001
            pass
        body: Any = None
        try:
            body = await web_request.json()
        except Exception:  # noqa: BLE001 - 老版本 json() 不支持默认值/无 body
            try:
                raw = await web_request.body()
                if raw:
                    import json as _json

                    body = _json.loads(raw)
            except Exception:  # noqa: BLE001
                body = None
        if isinstance(body, dict):
            for key, value in body.items():
                merged.setdefault(key, value)
        return merged

    async def _api_fallback(self, rest: str = "") -> Any:
        """兜底：按首段分发（桥可能带子路径，如 batch/preview）。"""
        head = (rest or "").split("/", 1)[0]
        for name, handler, _desc in self._route_defs():
            if name == head:
                return await handler()
        return error_response(f"未知的面板接口：{rest}", status_code=404)

    @staticmethod
    def _err(exc: Exception) -> Any:
        if isinstance(exc, (StoreError, PriorityConflictError, ValueError)):
            return error_response(str(exc), status_code=400)
        logger.error(f"[分组配置管家] 面板接口错误: {exc}", exc_info=True)
        return error_response(f"{type(exc).__name__}: {exc}", status_code=500)

    # ------------------------------------------------------------------
    # 概览 / 会话 / 资源池
    # ------------------------------------------------------------------
    async def api_overview(self) -> Any:
        data = self.store.data
        return json_response(
            {
                "ok": True,
                "data_dir": str(self.store.data_dir),
                "counts": {
                    "sessions": len(self.store.all_umos()),
                    "groups": len(data.get("groups") or {}),
                    "clusters": len(data.get("clusters") or {}),
                    "overrides": len(data.get("overrides") or {}),
                    "pins": sum(len(v) for v in (data.get("pins") or {}).values()),
                    "snapshots": len(self.store.list_snapshots()),
                },
            }
        )

    async def api_sessions(self) -> Any:
        try:
            p = await self._params()
            search = str(p.get("search") or "").lower()
            # 实时合并底层全量（ConversationV2 + 别名）与本插件登记簿：
            # 否则首次打开页面只看得到"消息事件逐条发现"的会话，要刷很多次。
            umos = set(self.store.all_umos())
            try:
                db_umos = await self.hooks.known_umos()
                if db_umos:
                    new = [u for u in db_umos if u not in umos]
                    umos.update(db_umos)
                    if new:
                        self.store.register_many(new, "db")
            except Exception as exc:  # noqa: BLE001 - 底层查询失败则退回登记簿
                logger.debug(f"[分组配置管家] sessions 合并底层全集失败: {exc}")
            groups = self.store.groups()
            out = []
            for umo in sorted(umos):
                if search and search not in umo.lower():
                    continue
                hit = [
                    {"id": g.id, "name": g.name, "priority": g.priority}
                    for g in sorted(
                        (g for g in groups.values() if group_matches(g, umo)),
                        key=lambda g: (g.priority, g.created_at),
                    )
                ]
                out.append(
                    {
                        "umo": umo,
                        "cluster": cluster_key(umo),
                        "is_sub": is_group_sub_session(umo),
                        "user": sub_session_user(umo),
                        "groups": hit,
                        "pinned": self.store.pins(umo),
                        "has_override": bool(self.store.override(umo))
                        and any(self.store.override(umo).values()),
                    }
                )
            return json_response({"sessions": out})
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    async def api_pools(self) -> Any:
        personas: list[dict[str, Any]] = []
        providers: list[dict[str, Any]] = []
        plugins: list[dict[str, Any]] = []
        try:
            for p in await self.context.persona_manager.get_all_personas():
                name = _pick(p, "name", "persona_id", default="")
                if name:
                    personas.append(
                        {"id": name, "desc": _pick(p, "description", "desc", default="")}
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 读取人格池失败: {exc}")
        try:
            # 主源：provider_manager.inst_map —— set_provider 校验的就是它的 key，
            # 用它保证下拉里的 id 一定能被底层接受
            pm = self.context.provider_manager
            inst_map = getattr(pm, "inst_map", None) or {}
            for pid, inst in inst_map.items():
                settings = getattr(inst, "provider_settings", None) or {}
                pname = ""
                if isinstance(settings, dict):
                    pname = str(settings.get("name") or settings.get("id") or "")
                providers.append({"id": str(pid), "name": pname or str(pid)})
            if not providers:
                # 回退：从 Provider 实例列表取（多字段兜底）
                for prov in self.context.get_all_providers():
                    pid = (
                        getattr(prov, "id", None)
                        or getattr(prov, "provider_id", None)
                        or ""
                    )
                    settings = getattr(prov, "provider_settings", None) or {}
                    if not pid and isinstance(settings, dict):
                        pid = settings.get("id") or ""
                    if pid:
                        pname = settings.get("name", "") if isinstance(settings, dict) else ""
                        providers.append({"id": str(pid), "name": str(pname or pid)})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 读取模型池失败: {exc}")
        # 检测"挂在 LLM / 消息链上的钩子类 handler"：核心的会话级插件开关
        # 只拦消息响应类 handler，钩子类（情绪/记忆注入等）拦不住——
        # 给前端提供 has_hooks，用来给"关"状态的插件打 ⚠ 标。
        hook_modules: set[str] = set()
        try:
            from astrbot.core.star.star_handler import (
                EventType,
                star_handlers_registry,
            )

            hook_types = {
                EventType.OnLLMRequestEvent,
                EventType.OnLLMResponseEvent,
                EventType.OnDecoratingResultEvent,
                EventType.OnAgentBeginEvent,
                EventType.OnAgentDoneEvent,
                EventType.OnWaitingLLMRequestEvent,
                EventType.OnUsingLLMToolEvent,
                EventType.OnLLMToolRespondEvent,
            }
            hook_modules = {
                getattr(h, "handler_module_path", "") or ""
                for h in star_handlers_registry
                if getattr(h, "event_type", None) in hook_types
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 扫描插件钩子失败: {exc}")

        try:
            from astrbot.core.star.star import star_map

            seen: set[str] = set()
            for md in star_map.values():
                name = getattr(md, "name", None)
                if not name or name in seen:
                    continue
                seen.add(name)
                module_path = str(getattr(md, "module_path", "") or "")
                has_hooks = bool(module_path) and any(
                    m == module_path or m.startswith(module_path + ".")
                    for m in hook_modules
                )
                plugins.append(
                    {
                        "name": name,
                        "activated": bool(getattr(md, "activated", True)),
                        "reserved": bool(getattr(md, "reserved", False)),
                        "desc": str(getattr(md, "desc", "") or ""),
                        "has_hooks": has_hooks,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 读取插件清单失败: {exc}")
        return json_response(
            {"personas": personas, "providers": providers, "plugins": plugins}
        )

    # ------------------------------------------------------------------
    # 分组
    # ------------------------------------------------------------------
    async def api_groups(self) -> Any:
        try:
            p = await self._params()
            action = str(p.get("action") or "")
            if not action:
                umos = self.store.all_umos()
                out = []
                for g in sorted(
                    self.store.groups().values(),
                    key=lambda g: (g.priority, g.created_at),
                ):
                    out.append(
                        {
                            "id": g.id,
                            "name": g.name,
                            "priority": g.priority,
                            "members": g.members,
                            "rules": g.rules,
                            "scope": g.scope,
                            "match_count": sum(
                                1 for u in umos if group_matches(g, u)
                            ),
                        }
                    )
                return json_response({"groups": out})

            synced = 0
            if action == "create":
                g = self.store.create_group(
                    str(p.get("name") or "未命名"),
                    priority=int(p.get("priority") or 100),
                )
                return json_response({"ok": True, "group": g.to_dict() | {"id": g.id}})
            gid = str(p.get("gid") or "")
            if action == "delete":
                self.store.delete_group(gid)
                return json_response({"ok": True})
            if action == "update":
                g = self.store.update_group(
                    gid,
                    name=p.get("name"),
                    priority=int(p["priority"]) if p.get("priority") is not None else None,
                )
                return json_response({"ok": True, "group": g.to_dict() | {"id": g.id}})
            if action == "add_member":
                # 支持单个（umo）与批量（umos 列表）
                umos = p.get("umos")
                if not isinstance(umos, list) or not umos:
                    umos = [str(p.get("umo") or "")]
                umos = [u for u in umos if u]
                added, failed = [], []
                for u in umos:
                    try:
                        self.store.add_member(gid, u)
                        added.append(u)
                    except PriorityConflictError as exc:
                        failed.append(str(exc))
                if added:
                    await self.engine.sync_many(added)
                return json_response(
                    {"ok": True, "added": len(added), "failed": failed}
                )
            if action == "remove_member":
                self.store.remove_member(gid, str(p.get("umo") or ""))
                synced = len(await self.engine.sync_many([str(p.get("umo") or "")]))
                return json_response({"ok": True, "synced": synced})
            if action == "add_rule":
                self.store.add_rule(gid, str(p.get("rule") or ""))
                targets = [
                    u
                    for u in self.store.all_umos()
                    if group_matches(self.store.get_group(gid), u)
                ]
                await self.engine.sync_many(targets)
                return json_response({"ok": True, "synced": len(targets)})
            if action == "remove_rule":
                self.store.remove_rule(gid, str(p.get("rule") or ""))
                # 规则移除后受影响的成员重算交由前端触发 sync（或下次全量对账）
                return json_response({"ok": True})
            if action == "set_scope":
                self.store.set_group_scope(
                    gid,
                    p.get("scope") or {},
                    replace=bool(p.get("replace")),
                    categories=p.get("categories") or None,
                )
                targets = [
                    u
                    for u in self.store.all_umos()
                    if group_matches(self.store.get_group(gid), u)
                ]
                await self.engine.sync_many(targets)
                return json_response({"ok": True, "synced": len(targets)})
            return error_response(f"未知 action: {action}", status_code=400)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    # ------------------------------------------------------------------
    # 作用域（全局 / 分组 / 整群 / 子会话）
    # ------------------------------------------------------------------
    async def _scope_target(self, p: dict[str, Any]) -> tuple[str, str]:
        layer = str(p.get("layer") or "")
        sid = str(p.get("id") or "")
        if layer not in ("global", "ungrouped", "group", "cluster", "session"):
            raise ValueError("layer 必须是 global/ungrouped/group/cluster/session")
        return layer, sid

    async def api_scope(self) -> Any:
        try:
            p = await self._params()
            layer, sid = await self._scope_target(p)
            if layer == "global":
                scope = self.store.global_scope()
            elif layer == "ungrouped":
                scope = self.store.ungrouped_scope()
            elif layer == "group":
                scope = self.store.get_group(sid).scope
            elif layer == "cluster":
                scope = self.store.cluster_scope(sid)
            else:
                scope = self.store.override(sid)
            if str(p.get("action") or "") == "save":
                delta = normalize_scope(p.get("scope"))
                replace = bool(p.get("replace"))
                categories = p.get("categories") or None
                if layer == "global":
                    self.store.set_global_scope(delta, replace=replace, categories=categories)
                    targets = list(self.store.all_umos())
                elif layer == "ungrouped":
                    self.store.set_ungrouped_scope(
                        delta, replace=replace, categories=categories
                    )
                    allg = list(self.store.groups().values())
                    targets = [
                        u
                        for u in self.store.all_umos()
                        if not any(group_matches(g, u) for g in allg)
                    ]
                elif layer == "group":
                    self.store.set_group_scope(sid, delta, replace=replace, categories=categories)
                    g = self.store.get_group(sid)
                    targets = [u for u in self.store.all_umos() if group_matches(g, u)]
                elif layer == "cluster":
                    self.store.set_cluster_scope(sid, delta, replace=replace, categories=categories)
                    # 目标集合与全局层对齐：登记簿 ∪ 底层全量再按群过滤，
                    # 避免登记簿缺漏导致该群部分会话没同步到禁用配置
                    merged = set(self.store.all_umos())
                    try:
                        merged.update(await self.hooks.known_umos())
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[分组配置管家] 群聊层合并底层失败: {exc}")
                    targets = [u for u in sorted(merged) if cluster_key(u) == sid]
                else:
                    self.store.set_override(sid, delta, replace=replace, categories=categories)
                    targets = [sid]
                reports = await self.engine.sync_many(targets)
                return json_response(
                    {
                        "ok": True,
                        "synced": len(targets),
                        "wrote": sum(len(r.wrote) for r in reports),
                        "drift": sum(len(r.drift) for r in reports),
                    }
                )
            return json_response({"scope": scope, "pinned": self.store.pins(sid) if layer == "session" else []})
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    # ------------------------------------------------------------------
    # 生效链
    # ------------------------------------------------------------------
    async def api_resolve(self) -> Any:
        try:
            p = await self._params()
            umo = str(p.get("umo") or "")
            if not umo:
                return error_response("缺少 umo", status_code=400)
            res = resolve(self.store.data, umo)
            native: dict[str, Any] = {}
            for key in (KEY_SESSION_SERVICE, KEY_PROVIDER_CHAT, KEY_SESSION_PLUGIN):
                native[key] = await self.engine.hooks.session_get(umo, key, None)
            groups_meta = []
            for gid in res.matched_groups:
                g = self.store.get_group(gid)
                groups_meta.append(
                    {"id": g.id, "name": g.name, "priority": g.priority}
                )
            # 人格源（决定性人格）：勾选层所选人格 = 一切数据的源头
            psrc_on = res.value(CATEGORY_PERSONA, "persona_source") is True
            decisive_pid = res.value(CATEGORY_PERSONA, "persona_id") if psrc_on else None
            return json_response(
                {
                    "umo": umo,
                    "cluster": cluster_key(umo),
                    "is_sub": is_group_sub_session(umo),
                    "user": sub_session_user(umo),
                    "matched_groups": groups_meta,
                    "values": res.values,
                    "sources": res.sources,
                    "override": self.store.override(umo),
                    "pinned": self.store.pins(umo),
                    "materialized": self.store.materialized(umo),
                    "native": native,
                    "decisive": {
                        "enabled": psrc_on,
                        "persona_id": decisive_pid,
                        "source": (
                            res.source(CATEGORY_PERSONA, "persona_id") if decisive_pid else None
                        ),
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    # ------------------------------------------------------------------
    # 同步 / 接管
    # ------------------------------------------------------------------
    async def api_sync(self) -> Any:
        try:
            p = await self._params()
            umo = str(p.get("umo") or "")
            if umo:
                rep = await self.engine.sync_session(umo)
                return json_response({"ok": True, "reports": [rep.summary()]})
            reports = await self.engine.reconcile_all()
            return json_response(
                {
                    "ok": True,
                    "total": len(reports),
                    "wrote": sum(len(r.wrote) for r in reports),
                    "drift": sum(len(r.drift) for r in reports),
                    "pinned_skipped": sum(len(r.pinned_skipped) for r in reports),
                    "errors": [r.summary() for r in reports if r.error][:5],
                }
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    async def api_takeover(self) -> Any:
        """重新交给分组管：清手动改过标记 + 清本插件写入记录 + 清单独设置，
        然后按 分组 → 群聊 → 全局 重新下发（takeover 式，不留比对基线）。"""
        try:
            p = await self._params()
            umo = str(p.get("umo") or "")
            if not umo:
                return error_response("缺少 umo", status_code=400)
            self.engine.takeover(umo, p.get("key") or None)
            self.store.clear_override(umo)
            self.store.clear_materialized(umo)
            rep = await self.engine.sync_session(umo, register=False)
            return json_response({"ok": True, "report": rep.summary()})
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    # ------------------------------------------------------------------
    # 批量（组内 / 补集）
    # ------------------------------------------------------------------
    def _batch_targets(self, mode: str, gid: str) -> list[str]:
        groups = self.store.groups()
        if gid not in groups:
            raise StoreError(f"分组 '{gid}' 不存在")
        umos = self.store.all_umos()
        if mode == "outside":
            return complement(umos, [groups[gid]])
        return [u for u in umos if group_matches(groups[gid], u)]

    async def api_batch(self) -> Any:
        try:
            p = await self._params()
            action = str(p.get("action") or "preview")
            if action == "rollback":
                name = str(p.get("name") or "")
                if not name:
                    snaps = self.store.list_snapshots()
                    if not snaps:
                        return error_response("没有可回滚的快照", status_code=400)
                    name = snaps[-1]
                self.store.restore_snapshot(name)
                reports = await self.engine.reconcile_all()
                return json_response(
                    {"ok": True, "restored": name, "synced": len(reports)}
                )

            mode = str(p.get("mode") or "in")
            if mode == "custom":
                # 自定义集合（如"未分类"）：前端直接给 umos
                targets = [str(u) for u in (p.get("umos") or []) if u]
                gid = ""
            elif mode in ("in", "outside"):
                gid = str(p.get("gid") or "")
                targets = self._batch_targets(mode, gid)
            else:
                return error_response("mode 必须是 in/outside/custom", status_code=400)
            delta = normalize_scope(p.get("scope"))
            if not any(delta.values()):
                return error_response("scope 为空，没有要应用的配置", status_code=400)
            if action == "preview":
                return json_response(
                    {"ok": True, "targets": targets, "count": len(targets), "scope": delta}
                )
            if action == "commit":
                if not targets:
                    return error_response("目标集合为空", status_code=400)
                snap = self.store.snapshot(f"web-{mode}-{gid}")
                for umo in targets:
                    self.store.set_override(umo, delta)
                reports = await self.engine.sync_many(targets)
                return json_response(
                    {
                        "ok": True,
                        "count": len(targets),
                        "wrote": sum(len(r.wrote) for r in reports),
                        "removed": sum(len(r.removed) for r in reports),
                        "drift": sum(len(r.drift) for r in reports),
                        "snapshot": snap.name,
                    }
                )
            return error_response(f"未知 action: {action}", status_code=400)
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    async def api_snapshots(self) -> Any:
        return json_response({"snapshots": self.store.list_snapshots()})

    async def api_clusters(self) -> Any:
        """群聊列表：每个物理群的整群设定与摘要（页③显示"是否已修改"）。"""
        try:
            raw = self.store.data.get("clusters") or {}
            out = []
            for ck in sorted(raw):
                scope = self.store.cluster_scope(ck)
                out.append(
                    {
                        "ck": ck,
                        "scope": scope,
                        "has_settings": any(scope.values()),
                        "persona_id": (scope.get("persona") or {}).get("persona_id"),
                        "provider_id": (scope.get("provider") or {}).get("provider_id"),
                        "service_count": sum(
                            1 for v in (scope.get("service") or {}).values() if v
                        ),
                        "disabled_count": len(
                            (scope.get("plugins") or {}).get("disabled_plugins") or []
                        ),
                    }
                )
            return json_response({"clusters": out})
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    async def api_factory_reset(self) -> Any:
        """完全恢复原状：删掉分组 / 整群 / 全局 / 单独设置等一切手动配置，
        并摘掉本插件写进原生层的受管字段（外部手改过的保留）。

        与 takeover 的区别：takeover 之后还有分组/群聊配置会重新下发；
        本接口清空定义层后不再下发任何东西，会话回到 AstrBot 原样。
        """
        try:
            cleared = {
                "groups": len(self.store.data.get("groups") or {}),
                "clusters": len(self.store.data.get("clusters") or {}),
                "overrides": len(self.store.data.get("overrides") or {}),
                "pins": sum(
                    len(v) for v in (self.store.data.get("pins") or {}).values()
                ),
            }
            # 1) 摘原生层：只摘"有物化记录且当前值与记录一致"（=我们写的、没被
            #    外部动过）的受管字段；外部手改过的尊重原样保留
            materialized = self.store.data.get("materialized") or {}
            targets = set(self.store.all_umos())
            try:
                targets.update(await self.hooks.known_umos())
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[分组配置管家] factory_reset 合并底层失败: {exc}")
            removed = 0
            for umo in sorted(targets):
                mat = materialized.get(umo) or {}
                # session_service_config（人格 + 服务开关）
                if KEY_SESSION_SERVICE in mat:
                    cur = await self.engine.hooks.session_get(
                        umo, KEY_SESSION_SERVICE, {}
                    )
                    cur = cur if isinstance(cur, dict) else {}
                    rec = mat[KEY_SESSION_SERVICE]
                    if project_managed(KEY_SESSION_SERVICE, cur) == project_managed(
                        KEY_SESSION_SERVICE, rec
                    ):
                        remaining = {
                            k: v
                            for k, v in cur.items()
                            if k not in SERVICE_MANAGED_FIELDS
                        }
                        if remaining:
                            await self.engine.hooks.session_put(
                                umo, KEY_SESSION_SERVICE, remaining
                            )
                        else:
                            await self.engine.hooks.session_remove(
                                umo, KEY_SESSION_SERVICE
                            )
                        removed += 1
                # provider（模型）
                if KEY_PROVIDER_CHAT in mat:
                    cur = await self.engine.hooks.session_get(
                        umo, KEY_PROVIDER_CHAT, None
                    )
                    if cur is not None and cur == mat[KEY_PROVIDER_CHAT]:
                        await self.engine.hooks.session_remove(
                            umo, KEY_PROVIDER_CHAT
                        )
                        removed += 1
                # session_plugin_config（插件开关）
                if KEY_SESSION_PLUGIN in mat:
                    cur = await self.engine.hooks.session_get(
                        umo, KEY_SESSION_PLUGIN, {}
                    )
                    cur = cur if isinstance(cur, dict) else {}
                    rec = mat[KEY_SESSION_PLUGIN]
                    if project_managed(KEY_SESSION_PLUGIN, cur) == project_managed(
                        KEY_SESSION_PLUGIN, rec
                    ):
                        remaining = dict(cur)
                        inner = dict(cur.get(umo) or {})
                        for fld in PLUGIN_MANAGED_FIELDS:
                            inner.pop(fld, None)
                        if inner:
                            remaining[umo] = inner
                        else:
                            remaining.pop(umo, None)
                        if remaining:
                            await self.engine.hooks.session_put(
                                umo, KEY_SESSION_PLUGIN, remaining
                            )
                        else:
                            await self.engine.hooks.session_remove(
                                umo, KEY_SESSION_PLUGIN
                            )
                        removed += 1

            # 2) 清空定义层（保留会话登记簿，它只是"见过哪些会话"的记录）
            self.store.data = self.store._default_data()
            self.store.save()

            # 3) 编辑器与页面状态由前端自行作废
            return json_response(
                {
                    "ok": True,
                    "cleared": cleared,
                    "removed_keys": removed,
                    "sessions": len(targets),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)

    async def api_reset_all(self) -> Any:
        """恢复为自动管理：清掉单独设置（overrides）与手动改过标记（pins）。

        - 带 umos 列表：只处理选中的会话；
        - 不带：处理全部会话。
        分组 / 群聊 / 全局的配置保留，处理后重新下发。
        """
        try:
            p = await self._params()
            umos = p.get("umos")
            selective = isinstance(umos, list) and bool(umos)
            if selective:
                targets = [str(u) for u in umos if u]
            else:
                merged = set(self.store.all_umos())
                try:
                    merged.update(await self.hooks.known_umos())
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[分组配置管家] reset_all 合并底层全集失败: {exc}")
                targets = sorted(merged)
            pins = self.store.data.setdefault("pins", {})
            overrides = self.store.data.setdefault("overrides", {})
            for u in targets:
                pins.pop(u, None)
                overrides.pop(u, None)
                # 关键：写入记录必须一起清（takeover 式），
                # 否则重新对账时旧记录一比对，标记立刻被打回来
                self.store.clear_materialized(u)
            self.store.save()
            reports = await self.engine.sync_many(targets)
            return json_response(
                {
                    "ok": True,
                    "scope": "selected" if selective else "all",
                    "synced": len(reports),
                    "wrote": sum(len(r.wrote) for r in reports),
                    "removed": sum(len(r.removed) for r in reports),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(exc)
