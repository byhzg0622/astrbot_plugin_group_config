"""人格源（决定性人格）枢纽：让所选人格成为一切数据的源头。

对应 docs/DESIGN.md 第 11 节。一句话：**勾选"人格源"的层级里所选的人格，
就是该会话的"决定性人格"——不仅 AstrBot 原生聊天用它，其他插件生成时
（主动消息、动态生活状态等自带人格选择的插件）最终拿到的也是它。**

数据面：
- scope 的 persona 类新增 `persona_source`（bool）字段，随五层继承
  （全局 / 分组 / 整群 / 子会话 四层各一个勾选框，未勾选的层向下继承）；
- 某 UMO 解析出 persona_source=true 且解析出 persona_id 时，该人格即
  "决定性人格"（decisive persona），来源层标注随生效链输出。

生效面（四路，全部只在"人格源开启"的会话/调用上生效，其余保持 AstrBot 原生）：
1. 物化 session_service_config.persona_id —— SyncEngine 已有路径，核心
   resolve_selected_persona 中该 key 优先级最高（force applied），普通聊天
   与其他走核心解析的插件天然跟随；
2. 对话人格对齐 conversation.persona_id —— "主动消息"这类插件读的是当前
   对话的 persona_id，同步时把它对齐为决定性人格（不新建对话；无对话时
   主动消息自建后走下面第 3 路回退）；
3. wrap persona_manager.get_default_persona_v3 —— 带 UMO 的调用返回该会话的
   决定性人格，无 UMO 的调用（如动态生活状态的回退路径）返回全局决定性人格；
4. wrap persona_manager.get_persona —— 仅当调用方是"人格跟随插件"
   （PERSONA_FOLLOWERS：主动消息 / 动态生活状态）时，重定向到全局决定性
   人格；其他调用方（WebUI 人格管理页、核心、本插件）一律原样放行。

安全约束（吸取 session_gate v3.0 的教训）：
- wrap 装在 persona_manager 单例的实例属性上，与核心 handler 绑定契约无关；
- 幂等安装：包装器带 __ps_wrap__/__ps_original__ 标记，本插件重载时不叠层，
  只把 pm._ps_hub 指针切到当前实例（包装器运行时按指针找 hub，旧实例的
  包装也能路由到新实例）；
- 可卸载：terminate 时还原原始方法；若发现 pm._ps_hub 已指向更新的实例，
  则完全不拆（不破坏新实例的包装）；
- 任何拦截路径异常都回落原始方法，绝不吞掉调用。
"""

from __future__ import annotations

import functools
import sys
from typing import Any

try:  # 独立单测环境（无 astrbot 包）下回落标准 logging
    from astrbot.api import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("group_config.personality")

from ..core.models import CATEGORY_PERSONA, normalize_scope
from ..core.resolver import resolve
from ..core.store import PluginStore

PLUGIN_NAME = "astrbot_plugin_group_config"

PERSONA_FOLLOWERS: frozenset[str] = frozenset({
    # 这两个插件的人格选择机制（本机 AstrBot 源码实测）：
    # - 主动消息：conversation.persona_id → get_persona；回退 get_default_persona_v3(umo)
    # - 动态生活状态：自己配置的 persona_id → get_persona；回退 get_default_persona_v3()
    # get_persona 无 UMO 可用，只能按"调用方插件名单"重定向（全局人格源开启时）。
    "astrbot_plugin_dynamic_life_state",
    "astrbot_plugin_proactive_chat",
})

_HUB_ATTR = "_ps_hub"          # persona_manager 实例上的当前 hub 指针
_WRAP_FLAG = "__ps_wrap__"     # 包装器标记（防叠层）
_ORIG_ATTR = "__ps_original__"  # 包装器上记录的原始方法（供卸载/接管）


def _active_plugin_module(max_depth: int = 10) -> str | None:
    """向上找第一个属于**其他** astrbot_plugin_* 的帧，返回插件根模块名。

    本插件自己的帧（wrap 处理体等）必须跳过，否则永远扫到自己。
    """
    try:
        frame = sys._getframe(1)
    except ValueError:
        return None
    depth = 0
    while frame is not None and depth < max_depth:
        name = frame.f_globals.get("__name__")
        if isinstance(name, str) and name.startswith("astrbot_plugin_"):
            root = name.split(".", 1)[0]
            if root != PLUGIN_NAME:
                return root
        frame = frame.f_back
        depth += 1
    return None


class PersonaSourceHub:
    """人格源枢纽：决定性人格解析 + 三处运行时生效（对话对齐 + 两个 wrap）。"""

    def __init__(
        self,
        store: PluginStore,
        hooks: Any,
        engine: Any,
        context: Any,
    ) -> None:
        self.store = store
        self.hooks = hooks
        self.engine = engine
        self.context = context
        self._pm: Any = None
        self._originals: dict[str, Any] = {}  # attr -> 原始方法

    # ------------------------------------------------------------------
    # 决定性人格
    # ------------------------------------------------------------------
    def decisive_persona(self, umo: str) -> tuple[str | None, str | None]:
        """返回 (决定性人格 id, 来源层标注)；未开启人格源或无人格时 (None, None)。

        语义：persona_source 与 persona_id 一起随五层继承；最终解析出
        persona_source=true 时，**最终解析出的 persona_id** 即决定性人格
        （高层可以只改人格不关开关，也可以只关开关不改人格）。
        """
        res = resolve(self.store.data, umo)
        if res.value(CATEGORY_PERSONA, "persona_source") is True:
            pid = res.value(CATEGORY_PERSONA, "persona_id")
            if isinstance(pid, str) and pid:
                return pid, res.source(CATEGORY_PERSONA, "persona_id")
        return None, None

    def global_decisive(self) -> tuple[str | None, str | None]:
        """UMO 无关的生成（动态生活状态这类全局生成）用的决定性人格。

        无会话上下文时只认**全局层**：全局层勾了人格源且有人格 → 该人格；
        否则 (None, None)，各调用方保持原生行为。
        """
        scope = normalize_scope(self.store.data.get("global_scope"))
        p = scope.get(CATEGORY_PERSONA) or {}
        if p.get("persona_source") is True:
            pid = p.get("persona_id")
            if isinstance(pid, str) and pid:
                return pid, "L1:global"
        return None, None

    # ------------------------------------------------------------------
    # 人格池查找（形状与原方法返回完全一致）
    # ------------------------------------------------------------------
    def _persona_manager(self) -> Any:
        if self._pm is not None:
            return self._pm
        pm = getattr(self.context, "persona_manager", None)
        if pm is not None:
            self._pm = pm
        return pm

    def _persona_dict(self, pid: str) -> Any:
        """personas_v3 中的 Personality（dict 形）；找不到返回 None。"""
        pm = self._persona_manager()
        if pm is None:
            return None
        for p in getattr(pm, "personas_v3", None) or []:
            try:
                if p["name"] == pid:
                    return p
            except Exception:  # noqa: BLE001
                continue
        return None

    def _persona_obj(self, pid: str) -> Any:
        """DB Persona 对象（get_persona 的返回形状）；找不到返回 None。"""
        pm = self._persona_manager()
        if pm is None:
            return None
        for p in getattr(pm, "personas", None) or []:
            if getattr(p, "persona_id", None) == pid:
                return p
        return None

    # ------------------------------------------------------------------
    # 对话人格对齐（覆盖"主动消息"这类读 conversation.persona_id 的插件）
    # ------------------------------------------------------------------
    async def align_conversation(self, umo: str) -> bool:
        """把当前对话的 persona_id 对齐为决定性人格；返回是否发生写入。

        - 无决定性人格 → 不动；
        - 无当前对话 → 不新建（主动消息自建的新对话 persona_id 为空，
          会走 get_default_persona_v3(umo) 回退，由 wrap 兜住）；
        - 值相同 → 不写。
        """
        pid, _src = self.decisive_persona(umo)
        if not pid:
            return False
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return False
        try:
            conv_id = await cm.get_curr_conversation_id(umo)
            if not conv_id:
                return False
            conv = await cm.get_conversation(umo, conv_id)
            if conv is None:
                return False
            if getattr(conv, "persona_id", None) == pid:
                return False
            await cm.update_conversation(umo, conv_id, persona_id=pid)
            logger.debug(f"[人格源] {umo} 对话人格已对齐为 {pid}")
            return True
        except Exception as exc:  # noqa: BLE001 - 对齐失败不影响主流程
            logger.debug(f"[人格源] 对话人格对齐失败 {umo}: {exc}")
            return False

    async def after_sync(self, umo: str) -> None:
        """SyncEngine.on_synced 回调：物化完成后对齐对话人格。"""
        await self.align_conversation(umo)

    # ------------------------------------------------------------------
    # wrap 安装 / 卸载（幂等；与 session_gate 同级别的可还原要求）
    # ------------------------------------------------------------------
    _WRAP_TARGETS = (
        ("get_default_persona_v3", "handle_get_default"),
        ("get_persona", "handle_get_persona"),
    )

    def install(self) -> int:
        """在 persona_manager 单例上安装拦截；返回新装的包装数（幂等）。"""
        pm = self._persona_manager()
        if pm is None:
            logger.debug("[人格源] context.persona_manager 不可用，跳过 wrap 安装")
            return 0
        setattr(pm, _HUB_ATTR, self)
        n = 0
        for name, handler in self._WRAP_TARGETS:
            n += self._install_one(pm, name, handler)
        return n

    def _install_one(self, pm: Any, name: str, handler_name: str) -> int:
        cur = getattr(pm, name, None)
        if cur is None:
            logger.debug(f"[人格源] persona_manager 无 {name}，跳过")
            return 0
        if getattr(cur, _WRAP_FLAG, False):
            # 历史实例留下的包装：只接管指针（包装器运行时按指针路由到本实例）
            orig = getattr(cur, _ORIG_ATTR, None)
            if orig is not None:
                self._originals[name] = orig
            return 0
        raw = cur

        @functools.wraps(raw)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            hub = getattr(pm, _HUB_ATTR, None)
            if hub is None:
                return await raw(*args, **kwargs)
            handler = getattr(hub, handler_name, None)
            if handler is None:
                return await raw(*args, **kwargs)
            return await handler(raw, *args, **kwargs)

        setattr(wrapper, _WRAP_FLAG, True)
        setattr(wrapper, _ORIG_ATTR, raw)
        try:
            setattr(pm, name, wrapper)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[人格源] 安装 {name} wrap 失败: {exc}")
            return 0
        self._originals[name] = raw
        return 1

    def uninstall(self) -> None:
        """还原全部 wrap；若已被更新的本插件实例接管则完全不拆。"""
        pm = self._pm
        if pm is None:
            return
        if getattr(pm, _HUB_ATTR, None) not in (self, None):
            return  # 新实例已接管：拆了会破坏它的拦截
        if getattr(pm, _HUB_ATTR, None) is self:
            try:
                delattr(pm, _HUB_ATTR)
            except Exception:  # noqa: BLE001
                setattr(pm, _HUB_ATTR, None)
        for name, original in self._originals.items():
            cur = getattr(pm, name, None)
            if getattr(cur, _WRAP_FLAG, False):
                try:
                    setattr(pm, name, original)
                except Exception:  # noqa: BLE001
                    pass
        self._originals.clear()

    # ------------------------------------------------------------------
    # wrap 处理体（异常一律回落原始方法，且原始方法只调用一次）
    # ------------------------------------------------------------------
    async def handle_get_default(
        self, original: Any, *args: Any, **kwargs: Any
    ) -> Any:
        """get_default_persona_v3 拦截：有 UMO 用该会话的决定性人格，
        无 UMO 用全局决定性人格；无人格源则原样。"""
        try:
            umo = kwargs.get("umo")
            if umo is None and args:
                umo = args[0]
            target: str | None
            if isinstance(umo, str) and umo:
                target = umo
            elif umo is not None:
                target = getattr(umo, "unified_msg_origin", None)
            else:
                target = None
            if target:
                pid, src = self.decisive_persona(target)
            else:
                pid, src = self.global_decisive()
            if pid:
                pd = self._persona_dict(pid)
                if pd is not None:
                    logger.debug(
                        f"[人格源] get_default_persona_v3 → {pid}（{src}）"
                    )
                    return pd
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[人格源] get_default_persona_v3 拦截失败: {exc}")
        return await original(*args, **kwargs)

    async def handle_get_persona(
        self, original: Any, *args: Any, **kwargs: Any
    ) -> Any:
        """get_persona 拦截：仅"人格跟随插件"的调用重定向到全局决定性人格。"""
        try:
            root = _active_plugin_module()
            if root and root != PLUGIN_NAME and root in PERSONA_FOLLOWERS:
                pid, src = self.global_decisive()
                requested = args[0] if args else kwargs.get("persona_id")
                if pid and requested != pid:
                    obj = self._persona_obj(pid)
                    if obj is not None:
                        logger.debug(
                            f"[人格源] {root} 取人格 {requested!r} → "
                            f"全局决定性人格 {pid!r}（{src}）"
                        )
                        return obj
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[人格源] get_persona 拦截失败: {exc}")
        return await original(*args, **kwargs)
