"""一次性还原器：把历史版本运行时包装过的 handler 全部换回原始函数。

## 为什么在 v1.9 彻底移除"运行时修改 handler"

本插件 v1.3~v1.8 曾在运行时把第三方插件的 handler 引用换成自定义包装器
（会话级拦截 + 参数对齐）。用户在自己环境做了对照实验，结论明确：

  1. 本插件自启 → **任何**插件重载后，该插件都会出现属于它自己的报错；
     随后停用本插件，报错依然存在；
  2. 本插件不自启 → 无论重不重载，都不报错。

机制上也解释得通：AstrBot 的插件重载（star_manager.reload）不换进程，
旧实例的闭包/引用不会消失；单插件重载又不清空 handler 注册表
（_unbind_plugin 仅在 smd.activated 为真时执行，_handlers 是 list 不去重），
于是"新旧两代 handler + 新旧两代包装器"必然搅在一起——只要"修改别人的
handler 引用"这件事存在，就没有彻底根治的办法。

结论：v1.9 起**彻底移除**运行时包装逻辑。本模块现在只做一件事——还原。

## v1.9 现在的行为

启动时（initialize）扫一遍 handler 注册表与 LLM 工具表，凡是被任意历史版本
守卫包装过的条目（识别标记 _gc_guarded，或 module 名含 hook_guard 的旧闭包），
一路坍缩回插件真正的原始函数并写回。之后本插件**不再触碰任何 handler**、
不注册任何后台任务、不修改任何 AstrBot 文件。
"""

from __future__ import annotations

import functools
from typing import Any, Callable

try:  # 测试环境没有 astrbot 包时退化为普通 logger
    from astrbot.api import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("gc_hook_guard")

_GUARD_ATTR = "_gc_guarded"  # 历史版本守卫包装层的标记
_ORIG_ATTR = "_gc_orig"  # 历史版本守卫保留的原始函数引用
_MAX_UNWRAP = 64  # 坍缩包装链的安全上限（防自引用死循环）


def _callable_in_closure(fn: Any) -> Callable | None:
    """返回闭包里第一个可调用的 cell 内容（旧版守卫闭包里那个 original）。"""
    for cell in getattr(fn, "__closure__", None) or ():
        value = getattr(cell, "cell_contents", None)
        if callable(value):
            return value
    return None


def _is_guard_wrapper(fn: Any) -> bool:
    """判断一个 handler 是不是（任意历史版本的）守卫包装层。"""
    if fn is None or not callable(fn):
        return False
    if getattr(fn, _GUARD_ATTR, False):
        return True  # 新旧版本守卫都会打这个标记
    if isinstance(fn, functools.partial):
        # 核心 star_manager.load() 会把"注册表里当前的 handler"再 partial 一个实例
        # （functools.partial 不转发属性，标记会被吞掉）。必须穿透这层才能认出来，
        # 否则既无法还原、也无法阻止 install() 叠加第二层包装。
        return _is_guard_wrapper(fn.func)
    module = getattr(fn, "__module__", "") or ""
    if "hook_guard" not in module:
        return False
    # 旧版守卫：async def guarded(*args, **kwargs)，闭包里持有 original；
    # 旧版只改 __name__ 不改 __qualname__，后者仍保留 …HookGuard…guarded 字样。
    qualname = getattr(fn, "__qualname__", "") or ""
    name_ = (getattr(fn, "__name__", "") or "") + qualname
    if "guard" not in name_.lower():
        return False
    return _callable_in_closure(fn) is not None


def _reapply_binding(recorded: Any, args: tuple) -> Any:
    """把 functools.partial 的绑定参数重新贴回"记录下来的原始引用"。

    核心绑定的参数就是插件实例；这样还原出来的值依旧是核心自己会放的形状
    （partial(未绑定函数, 实例)），而不是一个缺 self 的裸函数。
    """
    if not args:
        return recorded
    if isinstance(recorded, functools.partial):
        # 记录值本身就是绑定结果：用本次 partial 绑定的最新实例替换它自己的绑定
        if len(recorded.args or ()) == len(args):
            return functools.partial(recorded.func, *args)
        return recorded
    if getattr(recorded, "__self__", None) is not None:
        return recorded  # 绑定方法已经自带了实例，不能再贴
    if callable(recorded):
        return functools.partial(recorded, *args)
    return recorded


def _unwrap_guard(fn: Callable) -> Callable:
    """剥掉所有守卫包装层，返回插件真正的原始函数（bound method / partial / 函数）。

    只沿"守卫层"下钻：遇到非守卫函数立即停止，因此不会误伤插件自己用
    functools.wraps 包装的业务代码。核心 star_manager 会把注册表里的包装器
    再 partial 一个实例，这里必须穿透这层 partial（否则 v2.2 的残骸无法还原）。
    """
    seen: set[int] = set()
    for _ in range(_MAX_UNWRAP):
        if not _is_guard_wrapper(fn) or id(fn) in seen:
            break
        seen.add(id(fn))
        if isinstance(fn, functools.partial):
            inner = fn.func
            args = tuple(fn.args or ())
            recorded = (
                getattr(inner, _ORIG_ATTR, None)
                or getattr(inner, "__wrapped__", None)
                or _callable_in_closure(inner)
            )
            if recorded is None:
                break
            fn = _reapply_binding(recorded, args)
            continue
        inner = (
            getattr(fn, _ORIG_ATTR, None)
            or getattr(fn, "__wrapped__", None)
            or _callable_in_closure(fn)
        )
        if inner is None:
            break
        fn = inner
    return fn


class HookGuard:
    """还原器：只解包历史版本留下的包装，永不包装。"""

    def __init__(self) -> None:
        self.restored = 0  # 本次还原的条目数（handler + LLM 工具）

    async def cleanup(self) -> int:
        """扫描并还原所有被历史版本包装过的入口；幂等，返回还原数。"""
        self.restored = 0
        self._cleanup_handlers()
        self._cleanup_tools()
        return self.restored

    def _cleanup_handlers(self) -> None:
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 还原器：核心不可用，跳过: {exc}")
            return
        for md in star_handlers_registry:
            try:
                handler = md.handler
                if not _is_guard_wrapper(handler):
                    continue
                original = _unwrap_guard(handler)
                if original is handler:
                    continue  # 认得出标记但解不到底：不动，避免误伤
                md.handler = original
                self.restored += 1
                logger.info(
                    f"[分组配置管家] 已还原被历史版本包装的 handler："
                    f"{md.handler_module_path} - {md.handler_name}"
                )
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余
                logger.debug(f"[分组配置管家] 还原 handler 失败，跳过: {exc}")

    def _cleanup_tools(self) -> None:
        try:
            from astrbot.core.provider.register import llm_tools
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[分组配置管家] 还原器：工具表不可用，跳过: {exc}")
            return
        for tool in list(llm_tools.func_list):
            try:
                handler = getattr(tool, "handler", None)
                if not _is_guard_wrapper(handler):
                    continue
                original = _unwrap_guard(handler)
                if original is handler:
                    continue
                tool.handler = original
                self.restored += 1
                logger.info(
                    f"[分组配置管家] 已还原被历史版本包装的 LLM 工具：{tool.name}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[分组配置管家] 还原工具失败，跳过: {exc}")
