"""同步引擎：定义层五层解析 → AstrBot 原生会话规则（物化）。

对应 docs/DESIGN.md 第 5、6 节。

- 物化层 = AstrBot 原生 sp 规则（session_service_config / provider_perf_chat_completion /
  session_plugin_config），与官方"自定义规则"同构。
- AstrBot 侧依赖全部经 SyncHooks 注入：main.py 注入真实实现（sp、provider_manager、
  底层全集查询），单元测试注入内存假实现。
- 漂移检测：物化前比较 sp 当前值与上次记录值；不一致说明外部（如官方规则页）
  手改过 → 该 (umo, key) 自动 pin，停止物化，值以手改者为准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import (
    CATEGORY_PERSONA,
    CATEGORY_PLUGINS,
    CATEGORY_PROVIDER,
    CATEGORY_SERVICE,
    KEY_PROVIDER_CHAT,
    KEY_SESSION_PLUGIN,
    KEY_SESSION_SERVICE,
)

# 注：SERVICE_MANAGED_FIELDS / PLUGIN_MANAGED_FIELDS 定义于本文件下方常量区
from .resolver import resolve
from .store import PluginStore

# session_service_config 中受本插件管理的字段（其余如 custom_name 永不触碰）
SERVICE_MANAGED_FIELDS = ("persona_id", "llm_enabled", "tts_enabled", "session_enabled")
# session_plugin_config 受管理的字段
PLUGIN_MANAGED_FIELDS = ("enabled_plugins", "disabled_plugins")

_MISSING = object()


class SyncHooks(Protocol):
    """AstrBot 底层访问接口（由 main.py 提供真实现）。"""

    async def session_get(self, umo: str, key: str, default: Any) -> Any: ...

    async def session_put(self, umo: str, key: str, value: Any) -> None: ...

    async def session_remove(self, umo: str, key: str) -> None: ...

    async def set_provider(self, provider_id: str, umo: str) -> None: ...

    async def known_umos(self) -> list[str]: ...


@dataclass
class SessionSyncReport:
    """单会话同步报告。"""

    umo: str
    wrote: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    pinned_skipped: list[str] = field(default_factory=list)
    registered: bool = False
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.wrote or self.removed)

    def summary(self) -> str:
        parts: list[str] = []
        if self.registered:
            parts.append("新会话登记")
        if self.wrote:
            parts.append("已写入 " + ",".join(self.wrote))
        if self.removed:
            parts.append("已清除 " + ",".join(self.removed))
        if self.drift:
            parts.append("检测到外部修改并转手动覆盖 " + ",".join(self.drift))
        if self.pinned_skipped:
            parts.append("手动覆盖跳过 " + ",".join(self.pinned_skipped))
        if self.error:
            parts.append(f"错误: {self.error}")
        return f"{self.umo}: " + ("; ".join(parts) if parts else "无变化")


def project_managed(key: str, value: Any) -> Any:
    """只保留本插件管理的字段，用于漂移比较、记录与恢复原状。

    session_service_config 里还有官方字段（例如自动命名会写入
    custom_name），整 dict 比较会把这类无关变化误判成"外部手改"，
    导致所有会话被标成手动改过（pin）。必须按受管字段投影后再比。
    """
    if key == KEY_SESSION_SERVICE:
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if k in SERVICE_MANAGED_FIELDS}
    if key == KEY_SESSION_PLUGIN:
        if not isinstance(value, dict):
            return {}
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, dict):
                out[k] = {f: v[f] for f in PLUGIN_MANAGED_FIELDS if f in v}
            else:
                out[k] = v
        return out
    return value


class SyncEngine:
    """把定义层（PluginStore）的解析结果物化为 AstrBot 原生规则。"""

    def __init__(self, store: PluginStore, hooks: SyncHooks) -> None:
        self.store = store
        self.hooks = hooks
        # 物化完成后的回调（async def cb(umo: str) -> None）：main.py 注入
        # 人格源枢纽的对话人格对齐（conversation.persona_id 同步）。
        # 纯 Python 层不认识 AstrBot 运行时，一切运行时副作用经此回调进入。
        self.on_synced: Any = None

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def sync_session(self, umo: str, *, register: bool = True) -> SessionSyncReport:
        """同步单个会话（新会话自动登记全集）。单点失败不会中断批次。"""
        report = SessionSyncReport(umo=umo)
        if register and self.store.register_session(umo):
            report.registered = True
        # 不再自动打"手动改过"标记：本插件以定义层配置为准，同步即接管。
        # 顺手清掉历史遗留标记，避免改分组/全局后部分会话还挂着旧标记。
        if self.store.pins(umo):
            self.store.remove_pin(umo)
        try:
            resolved = resolve(self.store.data, umo)
            await self._sync_service(umo, resolved, report)
            await self._sync_provider(umo, resolved, report)
            await self._sync_plugins(umo, resolved, report)
        except Exception as exc:  # noqa: BLE001 - 物化失败必须隔离
            report.error = f"{type(exc).__name__}: {exc}"
        if self.on_synced is not None:
            try:
                await self.on_synced(umo)
            except Exception:  # noqa: BLE001 - 回调失败不影响物化结果
                pass
        return report

    async def sync_many(self, umos: list[str], *, register: bool = True) -> list[SessionSyncReport]:
        reports = []
        for umo in umos:
            reports.append(await self.sync_session(umo, register=register))
        return reports

    async def reconcile_all(self) -> list[SessionSyncReport]:
        """全量对账：底层已知会话 ∪ 登记簿，逐个同步（启动/手动全量同步用）。"""
        umos = set(await self.hooks.known_umos())
        umos.update(self.store.all_umos())
        return await self.sync_many(sorted(umos), register=True)

    def takeover(self, umo: str, key: str | None = None) -> None:
        """重新接管：清除 pin（及对应物化记录），下次同步按解析重物化。"""
        self.store.remove_pin(umo, key)
        if key is None:
            self.store.clear_materialized(umo)
        else:
            self.store.clear_materialized(umo, key)

    # ------------------------------------------------------------------
    # 漂移检测
    # ------------------------------------------------------------------
    # 注：原"漂移检测 → 自动打手动改过标记"机制已按用户要求移除。
    # 现在同步一律以定义层配置为准（物化覆盖），不再产生任何 pin；
    # 历史 pin 由 sync_session 开头清理。materialized 记录仍保留，
    # 用于"清除配置时只摘本插件写过的字段"。

    # ------------------------------------------------------------------
    # 三类物化
    # ------------------------------------------------------------------
    async def _sync_service(self, umo: str, resolved: Any, report: SessionSyncReport) -> None:
        """persona + service → session_service_config（同一 key，merge 写入）。"""
        key = KEY_SESSION_SERVICE
        current = await self.hooks.session_get(umo, key, {}) or {}
        if not isinstance(current, dict):
            current = {}

        # 显式重建受管字段：先摘掉全部受管字段，再写解析结果。
        # 人格与服务开关共用本 key——若直接 dict(current) 拷贝再覆盖，
        # "persona 全层无值但 service 有值"时 persona 残值会搭车保留，
        # 导致恢复自动后旧人格弹回、显示成"原生层"。
        target = dict(current)
        for _mk in SERVICE_MANAGED_FIELDS:
            target.pop(_mk, None)
        touched = False
        for cat in (CATEGORY_PERSONA, CATEGORY_SERVICE):
            for fname, val in (resolved.values.get(cat) or {}).items():
                if fname not in SERVICE_MANAGED_FIELDS:
                    continue  # persona_source（人格源）等纯标注字段不物化
                if current.get(fname) != val:
                    touched = True
                target[fname] = val

        mat = self.store.materialized(umo)
        recorded = mat.get(key, _MISSING)
        # 只看受管字段：persona_source 之类的标注字段不算"有值"
        has_value = any(
            fname in SERVICE_MANAGED_FIELDS
            for cat in (CATEGORY_PERSONA, CATEGORY_SERVICE)
            for fname in (resolved.values.get(cat) or {})
        )

        if not has_value:
            # 全层都未定义该类配置 → 无条件摘掉原生层里的受管字段，让 AstrBot
            # 回落原生默认（与"以本插件配置为准"一致；官方字段如 custom_name 保留）。
            # 不再依赖 materialized 记录：记录可能已被"完全恢复原状"清掉，
            # 若因此不摘，残值会永远显示"当前实际"，"恢复自动"看起来失效。
            if isinstance(current, dict) and any(
                k in current for k in SERVICE_MANAGED_FIELDS
            ):
                remaining = {
                    k: v for k, v in current.items() if k not in SERVICE_MANAGED_FIELDS
                }
                if remaining:
                    await self.hooks.session_put(umo, key, remaining)
                else:
                    await self.hooks.session_remove(umo, key)
                report.removed.append(key)
            self.store.clear_materialized(umo, key)
            return

        if touched or recorded is _MISSING or project_managed(key, target) != recorded:
            await self.hooks.session_put(umo, key, target)
            self.store.record_materialized(umo, key, project_managed(key, target))
            report.wrote.append(key)

    async def _sync_provider(self, umo: str, resolved: Any, report: SessionSyncReport) -> None:
        """provider → provider_perf_chat_completion（经底层 set_provider）。"""
        key = KEY_PROVIDER_CHAT
        current = await self.hooks.session_get(umo, key, None)

        pid = (resolved.values.get(CATEGORY_PROVIDER) or {}).get("provider_id")
        mat = self.store.materialized(umo)
        recorded = mat.get(key, _MISSING)

        if not pid:
            # 全层未定义模型 → 摘掉原生层的模型偏好，回落 AstrBot 默认
            if current is not None:
                await self.hooks.session_remove(umo, key)
                report.removed.append(key)
            self.store.clear_materialized(umo, key)
            return

        if current != pid or recorded is _MISSING:
            await self.hooks.set_provider(pid, umo)
            self.store.record_materialized(umo, key, pid)
            report.wrote.append(key)

    async def _sync_plugins(self, umo: str, resolved: Any, report: SessionSyncReport) -> None:
        """plugins → session_plugin_config（value 形如 {umo: {...}}，与官方同构）。"""
        key = KEY_SESSION_PLUGIN
        current = await self.hooks.session_get(umo, key, {}) or {}
        if not isinstance(current, dict):
            current = {}

        wanted = {f: list(resolved.values.get(CATEGORY_PLUGINS, {}).get(f) or [])
                  for f in PLUGIN_MANAGED_FIELDS}
        has_value = any(wanted.values())

        mat = self.store.materialized(umo)
        recorded = mat.get(key, _MISSING)

        if not has_value:
            # 全层未定义插件开关 → 摘掉本会话的受管字段（保留其他会话的数据）
            inner_now = (current.get(umo) or {}) if isinstance(current, dict) else {}
            if any(f in inner_now for f in PLUGIN_MANAGED_FIELDS):
                remaining = dict(current)
                inner_rm = dict(inner_now)
                for f in PLUGIN_MANAGED_FIELDS:
                    inner_rm.pop(f, None)
                if inner_rm:
                    remaining[umo] = inner_rm
                else:
                    remaining.pop(umo, None)
                if remaining:
                    await self.hooks.session_put(umo, key, remaining)
                else:
                    await self.hooks.session_remove(umo, key)
                report.removed.append(key)
            self.store.clear_materialized(umo, key)
            return

        inner = dict(current.get(umo) or {})
        inner.update(wanted)
        target = {**current, umo: inner}
        projected = project_managed(key, target)
        if recorded is _MISSING or recorded != projected:
            await self.hooks.session_put(umo, key, target)
            self.store.record_materialized(umo, key, projected)
            report.wrote.append(key)
