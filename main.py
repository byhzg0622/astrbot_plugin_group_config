"""分组配置管家（astrbot_plugin_group_config）——v3.1 新增「人格源」。

按分组集中管理每个群的人格、模型与插件开关配置：

- **五层继承**：内置 < 全局 < 分组(按优先级合并) < 整群 < 子会话覆盖，
  每层只存差量，未设置的字段自动向下继承（详见 docs/DESIGN.md）。
- **分组与补集**：分组可跨群收集会话（显式成员 + fnmatch 规则），
  支持"组内批量"与"组外（补集）批量"一键设置，带预览、二次确认与快照。
- **整群设定**：单独记忆下同一物理群的众多子会话共享整群配置，
  新子会话自动继承；每个子会话又可单独覆盖、可恢复继承。
- **双层存储**：定义层存本插件 data 目录；同步引擎把解析结果**物化**成
  AstrBot 原生会话规则（session_service_config / provider_perf_* /
  session_plugin_config），与官方"自定义规则"同构；官方页面手改会被
  漂移检测识别并转为手动覆盖（pin），可一键重新接管。

生命周期：
- __init__：加载定义层存储、装配同步引擎（底层经 hooks 注入）
- initialize：调度启动全量对账（底层已知会话 ∪ 登记簿 逐个物化）
- 新会话首条消息（priority=9000，先于其他插件）：登记 + 物化
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .api.webapi import WebApiMixin
from .core.apply import SyncEngine
from .core.hook_guard import HookGuard
from .core.session_gate import SessionGate, invalidate_session_cache
from .core.store import PluginStore
from .handlers.commands import CommandService
from .modules.personality import PersonaSourceHub

PLUGIN_NAME = "astrbot_plugin_group_config"


class AstrBotSyncHooks:
    """SyncHooks 的 AstrBot 真实现（进程内底层调用，不走 HTTP API）。"""

    def __init__(self, context: Context) -> None:
        self.context = context

    async def session_get(self, umo: str, key: str, default: Any = None) -> Any:
        from astrbot.core import sp

        return await sp.session_get(umo, key, default)

    async def session_put(self, umo: str, key: str, value: Any) -> None:
        from astrbot.core import sp

        await sp.session_put(umo, key, value)

    async def session_remove(self, umo: str, key: str) -> None:
        from astrbot.core import sp

        await sp.session_remove(umo, key)

    async def set_provider(self, provider_id: str, umo: str) -> None:
        from astrbot.core.provider.entities import ProviderType

        await self.context.provider_manager.set_provider(
            provider_id=provider_id,
            provider_type=ProviderType.CHAT_COMPLETION,
            umo=umo,
        )

    async def known_umos(self) -> list[str]:
        """底层全集：ConversationV2 去重 + UMO 别名（与官方"消息会话来源"同源）。"""
        from astrbot.core.db.po import ConversationV2
        from sqlmodel import select

        db = self.context.get_db()
        umos: set[str] = set()
        try:
            async with db.get_db() as session:
                result = await session.execute(
                    select(ConversationV2.user_id).distinct()
                )
                umos = {str(row[0]) for row in result.fetchall() if row[0]}
        except Exception as exc:  # noqa: BLE001 - 底层查询失败降级为空集
            logger.debug(f"[分组配置管家] 读取会话全集失败: {exc}")
        try:
            aliases = await db.get_umo_aliases()
            umos.update(str(a.umo) for a in aliases if getattr(a, "umo", None))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 读取 UMO 别名失败: {exc}")
        return sorted(umos)


class GroupConfigPlugin(WebApiMixin, Star):
    """分组配置管家插件主类（WebApiMixin 提供面板接口）。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        # 关键修复：核心 Star.__init__（base.py:28）不保存 self.config，必须自己存。
        # 之前漏了这行，_session_gate_enabled() 读 self.config 抛 AttributeError
        # 被 try 吞掉，开关永远是 False——v2.0~v2.2 期间闸门实际从未启用过。
        self.config = config if config is not None else {}
        self._data_dir = self._resolve_data_dir()
        self.store = PluginStore(self._data_dir)
        self.store.load()
        self.hooks = AstrBotSyncHooks(context)
        self.engine = SyncEngine(self.store, self.hooks)
        self.cmd = CommandService(self.store, self.engine)
        # 人格源枢纽（v3.1）：勾选"人格源"的层级所选人格 = 决定性人格，
        # 物化完成后把 conversation.persona_id 对齐（主动消息读它），
        # 并按需 wrap persona_manager（动态生活状态等插件取人格时重定向）。
        self.persona_hub = PersonaSourceHub(self.store, self.hooks, self.engine, context)
        self.engine.on_synced = self.persona_hub.after_sync
        self.hook_guard = HookGuard()  # 启动时一次性还原历史版本的旧包装
        self._gate_on = self._session_gate_enabled()  # 会话闸门开关，默认关闭
        self.session_gate = SessionGate()  # v3.0：调用瞬间拦截（与核心绑定契约同形）
        self.register_web_apis()

    def _session_gate_enabled(self) -> bool:
        """读取"会话级插件闸门"开关（配置缺失时默认关闭）。"""
        try:
            cfg = getattr(self, "config", None)
            return bool((cfg or {}).get("session_plugin_gate", False))
        except Exception as exc:  # noqa: BLE001 - 形态异常时保守取默认值并留痕
            logger.warning(
                f"[分组配置管家] 读取 session_plugin_gate 失败，按关闭处理: {exc}"
            )
            return False

    def _resolve_data_dir(self) -> Path:
        """数据目录：优先官方插件数据目录（防更新覆盖），回落插件内 data/。"""
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001
            return Path(__file__).resolve().parent / "data"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        logger.info(f"[分组配置管家] 已加载，数据目录：{self._data_dir}")
        logger.info(
            f"[分组配置管家] 会话闸门开关 session_plugin_gate = {self._gate_on}"
            "（配置页可改，变更后重载本插件生效）"
        )
        try:
            restored = await self.hook_guard.cleanup()
            if restored:
                logger.info(
                    f"[分组配置管家] 已还原 {restored} 个被历史版本（v1.3~v1.8）"
                    "运行时包装过的 handler/LLM 工具；本插件自 v1.9 起不再修改任何 handler"
                )
            else:
                logger.info("[分组配置管家] 无需还原：没有发现被历史版本包装的 handler")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[分组配置管家] 还原历史包装失败: {exc}")
        if self._gate_on:
            # v3.0：调用瞬间拦截——被禁用插件的 handler/LLM 工具在该会话里
            # 等同不存在；只过滤、不改任何参数。包装器与核心的绑定契约
            # （functools.partial(raw, 实例)）同形，核心重跑绑定循环也不会
            # 再出现 takes N but M（详见 core/session_gate.py 开头说明）。
            n = await self.session_gate.install()
            logger.info(
                f"[分组配置管家] 会话闸门已启用：接管 {n} 个 handler"
                f"（+{self.session_gate.tool_count} 个 LLM 工具），"
                "规则与核心自定义规则同源（session_plugin_config）"
            )
        n = self.persona_hub.install()
        logger.info(
            f"[分组配置管家] 人格源枢纽已安装：接管 {n} 个 persona_manager 方法"
            "（未勾选人格源的会话/调用保持 AstrBot 原生行为）"
        )
        try:
            self.context.register_task(
                self._startup_sync(),
                "分组配置管家-启动对账",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[分组配置管家] 启动对账调度失败: {exc}")

    async def _startup_sync(self) -> None:
        try:
            reports = await self.engine.reconcile_all()
            wrote = sum(len(r.wrote) for r in reports)
            drift = sum(len(r.drift) for r in reports)
            logger.info(
                f"[分组配置管家] 启动对账完成：{len(reports)} 个会话，"
                f"写入 {wrote} 处，漂移转手动覆盖 {drift} 处"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[分组配置管家] 启动对账失败: {exc}")

    async def terminate(self) -> None:
        try:
            self.persona_hub.uninstall()  # 还原 persona_manager 的两个人格方法
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 人格源 wrap 还原失败: {exc}")
        try:
            self.session_gate.uninstall()  # 把所有 handler/工具引用写回原始函数
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 会话闸门还原失败: {exc}")
        logger.info("[分组配置管家] 已卸载")

    # ------------------------------------------------------------------
    # 新会话接入：首条消息即登记 + 物化（高优先级，先于其他插件 handler）
    # ------------------------------------------------------------------
    @filter.on_plugin_loaded()
    async def on_any_plugin_loaded(self, metadata: Any) -> None:
        """新插件加载完成后增量接管它的 handler（幂等，无看门狗、无 repair）。"""
        if not self._gate_on:
            return
        try:
            added = await self.session_gate.install()
            if added:
                logger.info(f"[分组配置管家] 会话闸门增量接管 {added} 个新 handler")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 会话闸门增量安装失败: {exc}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9000)
    async def on_message_sync_session(self, event: AstrMessageEvent) -> None:
        try:
            umo = event.unified_msg_origin
            if umo in self.store.registry.get("sessions", {}):
                return  # 已知会话：配置变更由指令/页面侧同步，不在每条消息上重复
            rep = await self.engine.sync_session(umo)
            logger.info(f"[分组配置管家] 新会话接入：{rep.summary()}")
        except Exception as exc:  # noqa: BLE001 - 单条消息失败不影响聊天
            logger.debug(f"[分组配置管家] 会话同步失败: {exc}")

    # ------------------------------------------------------------------
    # 主指令：/groupcfg 或 /分组配置
    # ------------------------------------------------------------------
    @filter.command("groupcfg", alias={"分组配置", "gcfg"})
    async def cmd_groupcfg(self, event: AstrMessageEvent):
        """分组配置管家：/分组配置 help 查看全部子指令。"""
        try:
            text = await self.cmd.dispatch(
                self._tokens(event),
                event.unified_msg_origin,
            )
            invalidate_session_cache()  # 指令改了插件开关：立即生效，不等 TTL
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[分组配置管家] 指令出错: {exc}", exc_info=True)
            text = f"指令执行出错（{type(exc).__name__}）：{exc}"
        yield event.plain_result(text)

    @staticmethod
    def _tokens(event: AstrMessageEvent) -> list[str]:
        """把消息文本拆成指令 token（去掉前导斜杠与命令名本身）。"""
        text = (event.message_str or "").strip().lstrip("/").strip()
        parts = text.split()
        return parts[1:] if len(parts) > 1 else []
