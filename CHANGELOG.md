# 更新日志

> 当前版本 **v1.0.0**。本版在原版基础上新增「人格源」，插件名、指令、面板与数据目录与原版一致，整体替换原版目录即可升级。以下按版本倒序保留历史记录（v1.0.0 与 0.x 历史版本）；功能说明与使用方式见 [README.md](README.md)。

## v1.0.0 更新（新增）：人格源——所选人格是一切数据的源头

- **一个勾选框，四层可调**：全局 / 分组 / 整群 / 子会话 各有「人格源」勾选框
  （管理页人格卡；指令 `global/cluster/sub set src on|off`、`persona <名> src` 尾挂），
  随五层继承；`/分组配置 me` 与面板生效链显示 ★决定性人格及来源层。
- **勾选后，其他插件生成时选的也是你的决定性人格**：
  - 普通聊天：物化 `session_service_config.persona_id`（核心人格解析最高优先）；
  - 主动消息：对话人格 `conversation.persona_id` 同步对齐 + `get_default_persona_v3(umo)` 拦截；
  - 动态生活状态：`get_persona` 按"人格跟随插件"名单 + 全局人格源重定向，
    无 UMO 的回退路径取全局决定性人格。
- 不改任何第三方插件文件；wrap 幂等安装、可整层卸载；未勾选的会话/调用保持
  AstrBot 原生行为。机制详见 docs/DESIGN.md 第 11 节。

## v0.12 更新：修复 takes N but M 的真根因——包装器必须与核心绑定契约同形

现场日志（favour / 其他插件在"插件重启"后每条消息都报）：

    astrbot_plugin_group_config/core/session_gate.py:321, in guarded
        return await original(*args, **kwargs)      <-- 两帧同样的 guarded
        TypeError: FavourManagerTool._cache_event_for_active_chat()
                   takes 2 positional arguments but 3 were given

### 根因（两层，缺一不可）

1. **核心的绑定契约**：`star_manager.load()` 每次加载插件都会重跑
   （`astrbot/core/star/star_manager.py:1260-1276`）：

   ```python
   raw_handler = handler.handler.func if isinstance(handler.handler, functools.partial) else handler.handler
   handler.handler = raw_handler
   if not plugin_disabled and metadata.star_cls is not None:
       handler.handler = functools.partial(raw_handler, metadata.star_cls)
   ```

   即 **`md.handler` 一定是 `functools.partial(未绑定函数, 插件实例)`**。
   v0.10 把普通函数 `guarded` 写回 `md.handler`，核心就把它当未绑定函数再 partial
   一个实例。触发条件是"跳过 `_unbind_plugin` 的 load()"——最典型的是插件"重启"
   （禁用→启用）：`turn_on_plugin` 先把自己移出 `inactivated_plugins` 再 `reload()`，
   而此刻 `smd.activated` 仍是 False，于是 `star_manager.py:1066` 跳过解绑，
   模块也不出 sys.modules，`load()` 直接复用旧注册项并重跑绑定循环。
2. **partial 吞掉标记**：`functools.partial` 不转发属性查找，`_gc_guarded/_gc_orig`
   在它上面读不到 → `install()` 又叠一层（日志里两帧 `guarded`）、
   `uninstall()` 与 `HookGuard.cleanup()` 都拒绝还原 → 只能重启进程恢复。

这也解释了旧版"自启→任何插件重载后都报自己的错、停用本插件也没用"。

### 修复

包装器本身也装成 partial 形态：`md.handler = _BoundGate(gate, 实例)`，其中
`gate(实例, *args, **kwargs)` 把首参原样交给真正的未绑定函数。于是核心什么时候
重跑绑定循环都是**幂等**的：`partial(gate, 新实例)` → `gate(新实例, event)`
→ `raw(新实例, event)`，既不叠层也不可能重复传参。

- `core/session_gate.py`：`_BoundGate`（partial 子类，转发 `__name__`/`_gc_*`，
  刻意不转发 `__signature__`/`__wrapped__` 以保持 inspect 的 partial 语义）、
  `_split_bound`/`_our_gate`/`_binding_matches`，安装/还原全部按核心契约判定；
- `core/hook_guard.py`：`_is_guard_wrapper`/`_unwrap_guard` 现在能穿透
  `functools.partial`（v0.10 残骸可在不重启的情况下被 `cleanup()` 修好）；
- 签名/形态零退化：`inspect.signature(md.handler)` 与原生完全一致
  （如 `(event)`），`iscoroutinefunction`/`isasyncgenfunction` 仍是 True，
  `CommandFilter` 在装饰期缓存的参数表不受影响。

回归：`tests/test_session_gate.py::TestCoreRebindContract`（6 例，含用户现场的
install→核心重绑→再 install 时序、工具路径重绑、还原后仍可派发）+
`tests/test_hook_guard.py` 的 partial 残骸用例；全量 73 例通过，
并用真实 astrbot 核心类跑过一遍端到端验证。

## v0.11 更新：修复"闸门从未启用"的根因（漏存 self.config）

重载日志里没有"会话闸门已启用"一行，排查结果：

- **根因**：核心 `Star.__init__`（base.py:28）**并不保存** `self.config`——
  其他插件都在自己 `__init__` 里写 `self.config = config`，本插件漏了这一行；
  `_session_gate_enabled()` 读 `self.config` 抛 AttributeError 被 try 吞掉
  → 开关恒为 False → **v0.8 起的方案A/B、v0.10 的闸门实际从未启用过**
  （配置文件里明明是 `"session_plugin_gate": true`）。
- **修复**：`__init__` 保存 `self.config`；读取失败改为 warning 留痕（不再静默）；
  `initialize()` 增加一行 `会话闸门开关 session_plugin_gate = True/False`，
  开关状态一眼可见。

## v0.10 更新：换回"调用瞬间拦截"（只过滤、不改参数）——替换方案A

方案A（改写 event.plugins_name）依赖"本插件消息 handler 先执行 + 写入不抛异常"
两个前提，前提一破就静默失效——实测拦不住 对话分段Pro 的普通回复分段。
v0.10 换成旧版钩子守卫的可靠形态，并剔除它翻车的三个原因：

| 旧版翻车原因 | v0.10 的做法 |
| --- | --- |
| ①最早把 event 重复传入（takes N but M 通病） | **纯透传闭包**：只判断会话禁用，参数原样交给原函数，一个不动 |
| ②对齐/告警/看门狗（多实例互踩、层数越叠） | 全部没有：只在 initialize 装一次 + OnPluginLoadedEvent 增量装，**无看门狗、无 repair** |
| ③重载残留死实例条目 | 同名多条时把未登记的那条 `md.enabled=False` 中和 |

- **拦截时机**：handler/LLM 工具被调用的那一刻——覆盖 消息/指令、全部钩子
 （on_llm_request / on_decorating_result…）、结果装饰、LLM 工具四条路径，
 不依赖任何前置 handler 是否执行；
- **单层保证**：包装前先解包自己的旧包装（_gc_guarded/_gc_orig），重载不叠加；
- **完整还原**：terminate() 写回全部原始引用；开关 `session_plugin_gate` 关闭即完全退出；
- 数据源不变：session_plugin_config.disabled_plugins，与核心自定义规则同源。

仍然拦不到（无会话概念）：对话分段Pro 劫持 `Context.send_message` 的主动分段、
插件的 WebUI 路由、自建后台任务、Provider。

回归测试 63 个全过（新增：参数原样透传、洋葱模型静默、死条目中和、增量接管）。

## v0.9 更新：闸门可观测性 + 第二写入点（回应"对话分段Pro拦不住"，方案A 已被 v0.10 替换）

- **自检日志**：白名单每次真正生效时打一条 INFO（每会话每套禁用组合只打一次）：
  「会话闸门生效：<umo> 禁用 [...]；本会话白名单内第三方插件 N 个」——
  看到这条就说明方案A 在跑、被禁插件确实被排除了；看不到说明消息 handler
  没执行或闸门未启用。
- **第二写入点**：新增本插件的 `on_llm_request` 钩子再写一次白名单，
  兜底覆盖"消息链 handler 未执行但 LLM 钩子照跑"的合成事件场景。

已知拦不住的（查证自 对话分段Pro v1.4.8 源码）：
- 它 monkey-patch 了 `Context.send_message` 实例方法做**主动发送分段**
  （splitter/proactive.py:38），这条路完全绕过注册表分发，方案A/B 都拦不到
  ——旧版 hook_guard 同样拦不到（它只包 md.handler）。
  注意其 proactive 分段仅在 `split_scope != llm_only` 时生效，默认配置不活跃。

## v0.8 更新：会话级插件闸门（方案 A+B，默认关闭）

替代 v0.3~v0.6 的"运行时换 handler"，覆盖同样的四条路径，但**不修改任何
handler 引用**——从结构上规避重载互相拆台的问题：

| 路径 | 实现 | 接缝 |
| --- | --- | --- |
| 消息/指令 | 方案 A | 事件白名单（核心 waking_check 本就按 session 过滤） |
| 全部钩子（on_llm_request / on_llm_response / agent / tool 回调…） | 方案 A | 本插件 priority=9000 的消息 handler 里改写 event.plugins_name |
| 结果装饰 / 发送后 | 方案 A | 同上（result_decorate、call_event_hook 都传 plugins_name） |
| LLM 函数工具 | 方案 B | 单点补丁 astr_agent_tool_exec.call_local_llm_tool（卸载写回、重载拆旧装新保证单层） |

- **默认关闭**：配置 session_plugin_gate 打开后重载本插件生效；
- 数据源与核心自定义规则**同源**（session_plugin_config.disabled_plugins），
  分组配置里 off 掉哪个插件，这四条路径就拦哪个；
- 与全局"可用插件"白名单（plugin_set）取交集，不覆盖全局开关语义；
- 会话禁用表带 2 秒 TTL 缓存，执行 /分组配置 指令后立即失效。

拦不住的（无会话概念，任何方案都一样）：插件的 WebUI API 路由、插件自建的
后台 asyncio 任务、插件注册的 Provider。

回归测试：tests/test_session_gate.py（10 个用例）+ tests/test_hook_guard.py。

## v0.7 更新：彻底移除运行时 handler 修改（只保留一次性还原）

用户对照实验结论：

1. 本插件自启 → **任何**插件重载后，该插件都会出现自己的报错；停用本插件后报错仍在；
2. 本插件不自启 → 无论重不重载，都不报错。

机制上也吻合：AstrBot 插件重载不换进程，单插件重载又不清空 handler 注册表，
"新旧两代 handler + 新旧两代包装器"必然搅在一起——只要"修改别人的 handler
引用"存在，就没有彻底根治的办法。因此 v0.7：

- **移除**：运行时包装、会话闸门、参数对齐、后台自检任务、「/分组配置 guard」指令、
  「enable_hook_gate」配置项，全部删除；
- **保留**：启动时（initialize）一次性扫描，把被 v0.3~v0.6 包装过的 handler 与
  LLM 工具坍缩回原始函数并写回（core/hook_guard.py 现在是纯还原器）；
- 之后本插件**不再触碰任何 handler**，与 AstrBot 自带逻辑完全一致。

注意：v0.6 曾把"重载残留的重复条目"标记 md.enabled=False，升级后不回滚该标记——
那些条目指向已死的插件实例，回滚会让死实例复活。

## v0.6 更新：重载自愈（解释"重载后才报错、重启就干净"）

**机制**（源码依据 astrbot==4.28.2）：插件重载（`star_manager.reload`，1015-1071）
在同一进程里做"终止→解绑→重导入→重初始化"，旧实例的闭包/任务不会消失；
且单插件重载**不清空注册表**，`_unbind_plugin` 只在 `smd.activated` 为真时执行
（1066）、`terminate` 也只在 activated 为真时执行（1933）且与 `__del__` 是
if/elif（1944-1963）——一旦某条路径没走到，旧实例的 handler 条目就与新条目以
同一个 `handler_full_name` 共存（`_handlers` 是 list，append 不去重），死实例
继续被执行；两个 HookGuard 实例若同时动注册表，还会互相拆台（还原↔再包）。

v0.6 的三重自愈：

1. **归属权**：把 owner 用 weakref 打在**核心的** `star_handlers_registry` 上
  （它跨插件重载存活）；新实例接管后，旧实例识别出 `superseded` 即退避
 （install/repair 不再触碰注册表），其后台自检任务自动退出；
2. **死实例条目停用**：同一 `handler_full_name` 出现多条时，保留
  `star_handlers_map` 里登记的最新一条，其余 `md.enabled = False`
 （核心两处分发都过滤 enabled，见 `star_handler.py:161`、waking_check）；
3. **映射清理**：`_orig` 中已随重载消失的条目自动丢弃，不再持有死实例引用。

另完成并接通了并行会话留下的半成品：`_analyze` 新增的 `expects_self`
（未绑定函数、首参名为 self/cls 时，实例在首位是**正确**传法，不得误剥）
已接入两个包装器。回归测试 74 个全过。

## v0.5 更新：一键退回 AstrBot 自带逻辑（enable_hook_gate 开关）

不想继续跟着修？配置里新增总开关 `enable_hook_gate`（默认开）：

- **关闭后**：本插件不再接管任何执行入口，**完全退回 AstrBot 自带逻辑**
  （核心仅在 WakingCheck 过滤一次消息/指令 handler），分组/整群/子会话配置
  功能不受影响；运行中关闭会自动把已接管的入口还原干净，重载插件生效。
- **彻底退场**：停用/卸载本插件同样零残留（只改运行时引用，不改任何 AstrBot
  文件；`uninstall()` 还原到坍缩后的原始函数）。重启后旧包装闭包也随之消失。

注意：私聊里 `'XxxPlugin' object has no attribute 'unified_msg_origin'` 这类
报错**不是本插件造成的**——是有调用方把插件实例塞进了参数首位。

## v0.4 更新：会话内禁用 = 完全失效（底层执行闸门，已被 v0.7 移除）

核心自带的会话级插件过滤**只在 WakingCheck 做了一次**（`waking_check/stage.py:248`，
源码里 `filter_handlers_by_session` 仅此一处调用），其余执行路径全程不拦。v0.4 把闸门
下沉到插件的**全部执行入口**，会话内被禁用的插件在以下四条路径上全部被吞掉调用
（等同该 handler/工具不存在）：

| 路径 | 核心是否拦截 | v0.4 闸门 |
| --- | --- | --- |
| 消息/指令 handler（含异步生成器洋葱模型） | 仅 WakingCheck 一处 | ✅ 第二道保险 |
| 钩子类（`on_llm_request` / `on_decorating_result` / `on_after_message_sent`…） | ❌ | ✅ |
| LLM 函数工具（`FunctionTool.handler`，`@llm_tool` 注册） | ❌（存的是独立引用） | ✅ |
| 结果装饰/发送后钩子 | ❌ | ✅（并入钩子类） |

配套机制：会话开关查询带 2 秒 TTL 缓存（禁用最多 2 秒后全量生效）；`/分组配置` 指令
执行后与 `/分组配置 guard` 自检时会主动失效缓存，立即生效。

**管不到的边界**（AstrBot 当前架构限制，已在 `core/hook_guard.py` 文档注明）：插件注册的
WebUI API 路由没有会话概念；插件在 `initialize` 里自建的后台 asyncio 任务、注册的
Provider/适配器；插件自己把 event 转交给别的组件的调用。

## v0.3 更新：修复第三方插件通病「takes N positional arguments but M were given」

该通病（key_rotator / ai_mind / humanize_guard 等所有被守卫包装的插件同时
报 `TypeError: XxxPlugin.on_llm_request() takes 3 ... but 5 were given`）由本插件
旧版钩子守卫引起：旧包装把 event 重复传入，每多一层包装就多一个位置参数；
且旧代码对象会以闭包形式残留在运行中的进程里（重载不清除），所以报错持续。

v0.3 的 `core/hook_guard.py` 做了三重自愈（重载本插件即生效，无需重启）：

1. **坍缩包装链**：install 时把注册表 handler 上残留的任意层旧守卫解包回原始函数；
2. **保签名包装**：`functools.wraps` + `__signature__`，`iscoroutinefunction` 仍为 True；
3. **参数自适应**：按原始签名截断多余位置参数、丢弃未知关键字参数。

另附：`/分组配置 guard`（别名 repair/守卫/修复）手动触发一次自愈；
以及每 5 分钟一次的后台自检（`GUARD_WATCHDOG_INTERVAL`）。

回归测试：`tests/test_hook_guard.py`（插件根目录 `python -m unittest discover -s tests -t .`）。

## v0.2（历史版本）

### 新增
- **四页管理界面**：① 单独设置 ② 分组管理（按 a/b/c 分组卡片 + 未分类）③ 群聊设置（同群自动归卡）④ 全局设置（总览 + 数据维护）
- **未分类配置层**：未进任何分组的会话可用自己的统一设置（层级：全局 < 未分类 < 分组 < 整群 < 单独设置），带「编辑设置」编辑器
- 分组卡**搜索 + 多项勾选选人**、成员列表折叠（前 6 + 展开）、选中批量入组
- 群聊卡**「整群已设置」徽章**（显示人格/模型/插件改动摘要）、**「恢复默认」**（整群会话一键清单独设置）
- 会话多选（全选）+「恢复默认」批量入口
- 「最近动作」操作日志（localStorage 持久，同快照折叠样式）
- 生效链四类固定行 + **AstrBot 原生层"当前实际值"回显**（没配置也看得到此刻真正生效的人格/模型/开关）
- 「⚠ 完全恢复原状」（清掉分组等一切手动配置并把会话摘回原样）
- 未分类 / 分组 / 整群编辑器统一「恢复自动」与类别级保存

### 修复
- **移除"漂移检测自动打手动改过标记"**：改分组/全局不再给部分会话套标记；同步一律以本插件配置为准，历史标记随同步清除（同步开销同时大幅下降，响应明显变快）
- 受限 iframe 内原生 prompt/confirm 被禁用导致「新建分组」「一键恢复」点了没反应 → 全部改为页面内对话框
- 切换会话时编辑器复用上一个会话的草稿（设置不回显）→ 目标变更即重新拉取
- 编辑器改为显示**实际生效值**（原生层兜底 + 各层覆盖），保存只提交**动过的类别**，未动的继续跟随上层
- 会话列表一次刷新即可拉全（实时合并底层会话全集，无需连点多次）
- 选人列表/插件列表点击后滚动条跳动（重渲染丢滚动位置 → 统一保持）
- 模型下拉为空（Provider.id 取不到 → 改用 provider_manager.inst_map 权威来源）
- 生效链排版：第一列短类名不溢出、服务开关标注 聊天(LLM)/语音(TTS)/会话总开关、插件开关合并单行
- 会话自动命名写入 custom_name 导致整 dict 比较误判（曾造成全员"手动改过"，已随漂移机制一并移除）
- 清除配置时只摘本插件写过的字段，官方字段（custom_name 等）保留
- 「整群会话」（裸群号）不再出现在单独设置列表，归页③管理

### 底层（实现方式）
- 人格/模型/服务开关/插件开关全部走 AstrBot 原生规则（session_service_config / provider_perf_* / session_plugin_config），不使用 HTTP API
- 会话全集实时读取 ConversationV2 去重 + UMO 别名
- 插件按会话开关对接核心 SessionPluginManager（WakingCheck 阶段生效）

## v0.1（初版）
- 五层配置继承（内置 < 全局 < 分组 < 整群 < 子会话覆盖）、分组与补集批量、快照回滚、同步引擎、指令集与 WebUI 骨架
