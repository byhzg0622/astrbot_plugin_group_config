"""会话级插件闸门（v3.0）：在 handler 被调用的那一刻拦截——只过滤，不改任何参数。

## v3.0 相对 v2.2 的关键修复：包装器必须与核心的绑定契约同形

AstrBot 的 star_manager.load() 每次加载插件都会重跑绑定循环
（astrbot/core/star/star_manager.py:1260-1276）：

    raw_handler = handler.handler.func if isinstance(handler.handler, functools.partial) else handler.handler
    handler.handler = raw_handler
    if not plugin_disabled and metadata.star_cls is not None:
        handler.handler = functools.partial(raw_handler, metadata.star_cls)

它的隐含契约是：md.handler 一定是 functools.partial(未绑定函数, 插件实例)。

v2.2 把一个普通函数（guarded）写回 md.handler，于是只要核心之后再跑一次绑定循环，
核心就把"我们的包装器"当成未绑定函数，再 partial 一个新实例：

1. 插件"重启"（禁用→启用）时 turn_on_plugin 会先把自己移出 inactivated_plugins，
   再调 reload()；而此刻 smd.activated 仍是 False，于是 _unbind_plugin 被跳过
   （star_manager.py:1066），模块也不会被清出 sys.modules；
2. load() 命中 path in star_map，复用旧注册项并重跑绑定循环：
   md.handler = functools.partial(guarded, 新实例)；
3. 派发时 guarded(新实例, event) 再把参数原样转给"旧实例的绑定方法"，
   目标函数收到 3 个位置参数：
   TypeError: xxx() takes 2 positional arguments but 3 were given。

更糟的是 functools.partial **不转发属性查找**，_gc_guarded/_gc_orig 会被吞掉：
install() 会再叠一层（现场日志里出现两帧 guarded），而 uninstall() 与
HookGuard.cleanup() 都认不出它，只能重启进程才能恢复。

## v3.0 的做法

包装器本身也装成 partial 形态：md.handler = _BoundGate(gate, 实例)，其中
gate(实例, *args, **kwargs) 把首参原样交给真正的未绑定函数。这样：

- 正常派发：md.handler(event) → gate(实例, event) → raw(实例, event)；
- 核心重绑：raw_handler = md.handler.func（= gate）→ partial(gate, 新实例)
  → gate(新实例, event) → raw(新实例, event)。**幂等**：新实例自然流向原函数，
  既不叠加层数，也不可能重复传参——旧版 takes N but M 在结构上不可能再出现；
- 完整还原：uninstall() / HookGuard.cleanup() 都能识别 partial 形态的包装，
  写回安装前保存的绑定值。

其余特性与 v2.2 一致：只过滤、不改参数；没有看门狗、没有 repair；幂等安装；
terminate() 完整还原；整个功能挂在配置 session_plugin_gate 后面，关掉即退出。

数据源与核心自定义规则同源：session_plugin_config[umo].disabled_plugins
（sp.get_async 读取，与 SessionPluginManager 同规则），所以分组配置里
off 掉哪个插件，这里就拦哪个。

## 仍然拦不到的（无会话概念，任何方案都一样）

- 插件 monkey-patch 核心对象的路径（如 对话分段Pro 劫持 Context.send_message
  做主动发送分段）：不经过 handler/注册表，谁也拦不到；
- 插件注册的 WebUI API 路由、自建后台 asyncio 任务、Provider。
"""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any, Callable

try:  # 测试环境没有 astrbot 包时退化为普通 logger
    from astrbot.api import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("gc_session_gate")

from .hook_guard import _is_guard_wrapper, _unwrap_guard  # 复用 v1.9 的识别/坍缩

GATE_PLUGIN_NAME = "astrbot_plugin_group_config"

_GUARD_ATTR = "_gc_guarded"  # 本闸门包装层标记（与历史版本同名，便于互相识别）
_ORIG_ATTR = "_gc_orig"  # 包装层保存的"安装前"原始引用（还原/诊断用）
_RAW_ATTR = "_gc_raw"  # 闸门闭包里真正的未绑定函数

# 不包装的事件类型：加载/平台/错误类——没有会话概念，且核心对它们豁免过滤。
# AdapterMessageEvent（消息/指令 handler）**要包装**：核心只在 WakingCheck
# 过滤一次，这里作为第二道保险，且调用瞬间判断不依赖前置条件。
_SKIP_TYPE_NAMES = {
    "OnAstrBotLoadedEvent",
    "OnPlatformLoadedEvent",
    "OnPluginErrorEvent",
    "OnPluginLoadedEvent",
    "OnPluginUnloadedEvent",
}

# 会话禁用表缓存：每条消息每个 handler 都会查，2 秒内命中内存；
# 执行 /分组配置 指令后由 invalidate_session_cache() 立即失效。
_SESSION_CACHE_TTL = 2.0
_DISABLED_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
# 首次拦截留痕（每会话每插件只打一条 INFO，便于确认闸门真的在干活）
_SKIPPED_SEEN: set[tuple[str, str]] = set()


def invalidate_session_cache() -> None:
    """配置变更后调用：让"禁用/启用某插件"立即生效（否则最迟 TTL 后生效）。"""
    _DISABLED_CACHE.clear()
    _SKIPPED_SEEN.clear()


async def session_disabled_plugins(umo: str) -> frozenset[str]:
    """当前会话被禁用的插件名集合（与核心 SessionPluginManager 同源同规则）。"""
    if not umo:
        return frozenset()
    now = time.monotonic()
    hit = _DISABLED_CACHE.get(umo)
    if hit and now - hit[0] < _SESSION_CACHE_TTL:
        return hit[1]
    from astrbot.core import sp

    raw = await sp.get_async(
        scope="umo",
        scope_id=umo,
        key="session_plugin_config",
        default={},
    )
    session_cfg = (raw or {}).get(umo, {}) or {}
    disabled = frozenset(str(n) for n in (session_cfg.get("disabled_plugins") or []))
    if len(_DISABLED_CACHE) > 4096:
        _DISABLED_CACHE.clear()
    _DISABLED_CACHE[umo] = (now, disabled)
    return disabled


def _find_event(args: tuple, kwargs: dict) -> Any:
    """从调用参数里找消息事件（钩子/消息 handler：事件在首位；工具：event 打头；
    "call" 风格工具：事件在 context.context.event）。"""
    try:
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
    except Exception:  # noqa: BLE001 - 环境不完整时交给上层放行
        return None
    for a in args:
        if isinstance(a, AstrMessageEvent):
            return a
        inner = getattr(a, "event", None) or getattr(
            getattr(a, "context", None), "event", None
        )
        if isinstance(inner, AstrMessageEvent):
            return inner
    for v in kwargs.values():
        if isinstance(v, AstrMessageEvent):
            return v
        inner = getattr(v, "event", None)
        if isinstance(inner, AstrMessageEvent):
            return inner
    return None


async def _plugin_enabled(plugin_name: str, args: tuple, kwargs: dict) -> bool:
    """当前会话是否允许运行该插件。找不到事件时放行（无会话概念的调用）。"""
    event = _find_event(args, kwargs)
    umo = getattr(event, "unified_msg_origin", None) if event else None
    if not umo:
        return True
    disabled = await session_disabled_plugins(umo)
    if plugin_name in disabled:
        key = (umo, plugin_name)
        if key not in _SKIPPED_SEEN:
            if len(_SKIPPED_SEEN) > 4096:
                _SKIPPED_SEEN.clear()
            _SKIPPED_SEEN.add(key)
            logger.info(
                f"[分组配置管家] 会话闸门：{plugin_name} 在 {umo} 被禁用，已拦截其调用"
            )
        return False
    return True


# ---------------------------------------------------------------------------
# 与核心绑定契约同形的包装器
# ---------------------------------------------------------------------------

# _BoundGate 只转发这几项：刻意不转发 __signature__（让 inspect.signature 走
# partial 语义，自动扣掉已被绑定的首参），也不转发 __wrapped__（inspect.unwrap
# 会顺着它跳到闸门函数上，从而绕过 partial 语义拿到未扣参的签名）。
_FORWARD_ATTRS = (
    "__name__",
    "__qualname__",
    "__doc__",
    _GUARD_ATTR,
    _ORIG_ATTR,
    _RAW_ATTR,
)


class _BoundGate(functools.partial):
    """核心同形的包装：functools.partial(gate, 插件实例)。

    核心只用 isinstance(x, functools.partial) 判定"已绑定"并取 .func，
    所以本类与核心自己产生的 partial 完全同形；额外把少量属性转发给闸门函数，
    让 __name__/_gc_guarded 等外部可见性不退化。
    """

    def __getattr__(self, item: str) -> Any:  # 仅在常规查找失败时触发
        if item in _FORWARD_ATTRS:
            return getattr(self.func, item)
        raise AttributeError(item)


def _split_bound(handler: Any) -> tuple[Any, Any]:
    """把核心的绑定形状拆成 (未绑定函数, 插件实例)。

    - 核心形状 functools.partial(raw, instance) -> (raw, instance)
    - 绑定方法 instance.method                 -> (method.__func__, instance)
    - 其它（模块级函数 / 无法判定）             -> (handler, None)
    """
    if isinstance(handler, functools.partial):
        func = handler.func
        args = tuple(handler.args or ())
        self_obj = getattr(func, "__self__", None)
        if self_obj is not None:  # partial 里包着绑定方法
            return getattr(func, "__func__", func), (args[0] if args else self_obj)
        return func, (args[0] if args else None)
    self_obj = getattr(handler, "__self__", None)
    if self_obj is not None:
        return getattr(handler, "__func__", handler), self_obj
    return handler, None


def _our_gate(handler: Any) -> tuple[Callable, Callable] | None:
    """若 handler 是本闸门安装的包装（plain 或 partial 形态），返回 (gate, raw)。"""
    if handler is None:
        return None
    if isinstance(handler, functools.partial):
        func = handler.func
        raw = getattr(func, _RAW_ATTR, None)
        return (func, raw) if raw is not None else None
    raw = getattr(handler, _RAW_ATTR, None)
    return (handler, raw) if raw is not None else None


def _wrap_target(raw: Callable, instance: Any) -> Any:
    """核心在"没有我们包装"时会放在 md.handler 上的值。"""
    return functools.partial(raw, instance) if instance is not None else raw


def _binding_matches(saved: Any, raw: Callable, instance: Any) -> bool:
    """saved 是否已经就是 raw 绑在 instance 上的形态。"""
    if isinstance(saved, functools.partial):
        args = tuple(saved.args or ())
        return saved.func is raw and len(args) == 1 and args[0] is instance
    return (
        getattr(saved, "__self__", None) is instance
        and getattr(saved, "__func__", None) is raw
    )


def _make_gate(raw: Callable, plugin_name: str) -> Callable:
    """核心绑定形态的闸门：gate(instance, *args, **kwargs) 原样转给 raw。

    因为首参永远是核心绑定的插件实例，核心之后再
    functools.partial(gate, 新实例) 也只是换了首参——依旧正确、且不会叠加。
    """
    try:
        raw_sig = inspect.signature(raw)
    except (TypeError, ValueError):  # noqa: BLE001 - 拿不到签名就退化为不设
        raw_sig = None

    if inspect.isasyncgenfunction(raw):

        @functools.wraps(raw)
        async def gate(instance: Any, *args: Any, **kwargs: Any):
            try:
                if not await _plugin_enabled(plugin_name, args, kwargs):
                    return  # 禁用：等同 handler 不存在，一个 yield 都不产出
            except Exception as exc:  # noqa: BLE001 - 闸门出错必须放行
                logger.debug(f"[分组配置管家] 会话检查失败，放行: {exc}")
            async for item in raw(instance, *args, **kwargs):  # 参数原样透传
                yield item

    else:

        @functools.wraps(raw)
        async def gate(instance: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                if not await _plugin_enabled(plugin_name, args, kwargs):
                    return None  # 禁用：等同 handler 不存在
            except Exception as exc:  # noqa: BLE001 - 闸门出错必须放行
                logger.debug(f"[分组配置管家] 会话检查失败，放行: {exc}")
            return await raw(instance, *args, **kwargs)  # 参数原样透传

    if raw_sig is not None:
        # 记录"未绑定"的完整签名（含 self）：核心 partial 掉首参后，
        # inspect.signature(md.handler) 会自动扣掉 self，与原生完全一致。
        gate.__signature__ = raw_sig
    setattr(gate, _GUARD_ATTR, True)
    setattr(gate, _RAW_ATTR, raw)
    return gate


def _make_plain_gate(raw: Callable, plugin_name: str) -> Callable:
    """无实例形态（模块级 handler / 无法判定绑定）：参数原样透传。

    核心对这类 handler 也会 functools.partial(raw, 实例)；因为闸门不吞参数，
    gate(实例, event) 会原样转成 raw(实例, event)，依旧是核心期望的调用。
    """
    try:
        raw_sig = inspect.signature(raw)
    except (TypeError, ValueError):  # noqa: BLE001
        raw_sig = None

    if inspect.isasyncgenfunction(raw):

        @functools.wraps(raw)
        async def guarded(*args: Any, **kwargs: Any):
            try:
                if not await _plugin_enabled(plugin_name, args, kwargs):
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[分组配置管家] 会话检查失败，放行: {exc}")
            async for item in raw(*args, **kwargs):
                yield item

    else:

        @functools.wraps(raw)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                if not await _plugin_enabled(plugin_name, args, kwargs):
                    return None
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[分组配置管家] 会话检查失败，放行: {exc}")
            return await raw(*args, **kwargs)

    if raw_sig is not None:
        guarded.__signature__ = raw_sig
    setattr(guarded, _GUARD_ATTR, True)
    setattr(guarded, _RAW_ATTR, raw)
    return guarded


class SessionGate:
    """调用瞬间拦截：被禁用插件的 handler/LLM 工具在该会话里等同不存在。

    只过滤、不改任何参数；不持有后台任务；卸载时完整还原；
    包装器形态与核心 functools.partial(raw, instance) 的绑定契约同形，
    因此核心在任何时候重跑绑定循环都是幂等的（详见模块开头说明）。
    """

    def __init__(self) -> None:
        # handler_full_name -> 我们替换前的引用（还原用）
        self._orig: dict[str, Callable] = {}
        # tool name -> 我们替换前的 FunctionTool.handler
        self._tool_orig: dict[str, Callable] = {}
        # (id(未绑定函数), 插件名, 形态) -> 闸门函数（同一函数复用同一个闸门）
        self._wrap_cache: dict[tuple[int, str, int], Callable] = {}

    # ------------------------------------------------------------------
    # 安装 / 卸载（没有看门狗、没有 repair：装一次 + 增量，幂等）
    # ------------------------------------------------------------------
    async def install(self) -> int:
        added = self._install_handlers()
        added += self._install_tools()
        return added

    def _gated(self, raw: Callable, plugin_name: str, instance: Any, marker: Any) -> Any:
        """按核心契约装一层闸门：有实例就装 partial 形态，否则装无实例形态。"""
        shape = 1 if instance is not None else 0
        cache_key = (id(raw), plugin_name, shape)
        gate = self._wrap_cache.get(cache_key)
        if gate is None or getattr(gate, _RAW_ATTR, None) is not raw:
            gate = (
                _make_gate(raw, plugin_name)
                if instance is not None
                else _make_plain_gate(raw, plugin_name)
            )
            self._wrap_cache[cache_key] = gate
        if getattr(gate, _ORIG_ATTR, None) is None:
            setattr(gate, _ORIG_ATTR, marker)
        return _BoundGate(gate, instance) if instance is not None else gate

    def _install_one(self, key: str, holder: Any, plugin_name: str, saved_map: dict) -> int:
        """把 holder.handler 换成同形闸门；返回 1 表示本次有改动。"""
        cur = getattr(holder, "handler", None)
        if not callable(cur):
            return 0

        ours = _our_gate(cur)
        if ours is not None:
            gate, raw = ours
            if raw is None:
                return 0
            instance = _split_bound(cur)[1]
            saved = saved_map.get(key)
            if saved is not None:
                # 本实例已经装过：核心可能刚换了绑定实例 -> 只刷新还原目标，形态不用动
                if instance is not None and not _binding_matches(saved, raw, instance):
                    saved_map[key] = _wrap_target(raw, instance)
                return 0
            # 另一个 SessionGate 实例（旧实例被重载且 terminate 没跑到）装的：
            # 复用同一个未绑定函数重装一层，保持单层，绝不叠加
            saved_map[key] = cur
            marker = getattr(gate, _ORIG_ATTR, None)
            holder.handler = self._gated(
                raw, plugin_name, instance, marker if marker is not None else cur
            )
            return 1

        # 历史版本（v1.3~v2.2）留下的包装：先坍缩回真正的原始函数再装
        origin = _unwrap_guard(cur) if _is_guard_wrapper(cur) else cur
        raw, instance = _split_bound(origin)
        if not callable(raw):
            return 0
        saved_map[key] = origin
        holder.handler = self._gated(raw, plugin_name, instance, origin)
        return 1

    def _install_handlers(self) -> int:
        try:
            from astrbot.core.star.star import star_map
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 会话闸门：核心不可用，跳过: {exc}")
            return 0

        # 死条目中和：核心单插件重载不清注册表（_handlers 是 list 不去重），
        # 同名多条时保留 star_handlers_map 登记的最新一条，其余停用。
        latest_map = getattr(star_handlers_registry, "star_handlers_map", None) or {}
        counts: dict[str, int] = {}
        for md in star_handlers_registry:
            counts[md.handler_full_name] = counts.get(md.handler_full_name, 0) + 1
        dup_keys = {k for k, c in counts.items() if c > 1}

        added = 0
        for md in star_handlers_registry:
            try:
                key = md.handler_full_name
                if key in dup_keys:
                    latest = latest_map.get(key)
                    if latest is not None and latest is not md:
                        if getattr(md, "enabled", True):
                            md.enabled = False
                            logger.warning(
                                f"[分组配置管家] 会话闸门：{md.handler_module_path} - "
                                f"{md.handler_name} 存在重载残留的重复条目，已停用该条"
                            )
                        continue
                if getattr(md.event_type, "name", "") in _SKIP_TYPE_NAMES:
                    continue
                plugin = star_map.get(md.handler_module_path)
                if plugin is None or getattr(plugin, "reserved", False):
                    continue
                name = getattr(plugin, "name", None)
                if not name or name == GATE_PLUGIN_NAME:
                    continue  # 不拦内置插件与本插件自己
                added += self._install_one(key, md, name, self._orig)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余
                logger.debug(f"[分组配置管家] 包装 handler 失败，跳过: {exc}")
        return added

    def _install_tools(self) -> int:
        try:
            from astrbot.core.provider.register import llm_tools
            from astrbot.core.star.star import star_map
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 会话闸门：工具表不可用，跳过: {exc}")
            return 0

        added = 0
        for tool in list(llm_tools.func_list):
            try:
                if getattr(tool, "handler", None) is None:
                    continue  # MCP / run 风格工具，不属于插件执行入口
                module_path = getattr(tool, "handler_module_path", None)
                plugin = star_map.get(module_path) if module_path else None
                if plugin is None or getattr(plugin, "reserved", False):
                    continue
                name = getattr(plugin, "name", None)
                if not name or name == GATE_PLUGIN_NAME:
                    continue
                # 与 handler 同一套闸门：镜像原函数形态（协程/异步生成器）、
                # 只过滤不改参数；工具执行时 event 在首位，会话可判定
                added += self._install_one(tool.name, tool, name, self._tool_orig)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[分组配置管家] 包装工具失败，跳过: {exc}")
        return added

    def uninstall(self) -> None:
        """还原所有包装（terminate 时调用）：写回我们替换前的原始引用。"""
        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            for md in star_handlers_registry:
                saved = self._orig.get(md.handler_full_name)
                if saved is not None and _our_gate(md.handler) is not None:
                    md.handler = saved
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 还原 handler 失败: {exc}")
        try:
            from astrbot.core.provider.register import llm_tools

            for tool in llm_tools.func_list:
                saved = self._tool_orig.get(tool.name)
                if (
                    saved is not None
                    and _our_gate(getattr(tool, "handler", None)) is not None
                ):
                    tool.handler = saved
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 还原工具失败: {exc}")
        self._orig.clear()
        self._tool_orig.clear()
        self._wrap_cache.clear()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def wrapped_count(self) -> int:
        return len(self._orig)

    @property
    def tool_count(self) -> int:
        return len(self._tool_orig)
