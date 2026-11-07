# 详细设计（DESIGN）

> 本文档只描述设计，不含代码实现。实现以本文件为准；有出入先改文档再改实现。
> 底层调研基于本机 AstrBot 源码：`D:\bot\AstrBotLauncher-0.3.0\AstrBot`。

## 1. 术语表

| 术语 | 英文/标识 | 含义 |
| --- | --- | --- |
| 会话 | Session | 配置生效的最小单位。key 直接取消息事件的 unified_msg_origin（UMO），原样存储。实测格式：`default:GroupMessage:1225425596_1053676331` |
| UMO | unified_msg_origin | 形如 `[platform]:[message_type]:[session_id]`（split(":", 2) 三段，与核心 UmopConfigRouter 一致）。**实测四种形态**（用户确认）：`default:GroupMessage:本人QQ号_群号`、`default:GroupMessage:群号`（裸群会话）、`default:FriendMessage:QQ号`（私聊）、`webchat:FriendMessage:webchat代号` |
| 物理群 | Cluster | 一个平台群聊。解析规则：message_type 含 "group" 时，session_id 形如 `本人QQ号_群号`——**群号在 `_` 之后**（前段是发送者 QQ）；无 `_` 则整段即群号。例：`1225425596_1053676331` → 群 `1053676331`。cluster key = `[platform]::group::[群号]`（message_type 归一为 group），私聊/webchat 返回 None。实现见 `models.cluster_key`，整群匹配规则见 `models.cluster_rule_patterns`（裸 + 子会话两条模式） |
| 子会话 | Sub-session | "单独记忆"（AstrBot 内部自带逻辑）下物理群中互相隔离的记忆单元：session_id 带 `_群号` 后缀的 UMO，前段为发送者 QQ；末段不带 `_` 的是该群整体（裸）会话。例：`1225425596_1053676331` 与裸 `1053676331` 同属群 `1053676331` |
| 分组 | Group | 用户自定义**逻辑**分组（a/b/c…），带整数 priority。成员 = 显式 UMO + fnmatch 通配规则（语义同核心 UMOP：如 `default:GroupMessage:1225425596_*`） |
| 作用域 | Scope | 某一层保存的配置差量，四类：persona / provider / service / plugins |
| 覆盖 | Override | 显式写在 L4（子会话）层、或经漂移检测"脱离管理"的字段 |
| 继承 | Inherit | 本层无该字段，解析时取更底层结果 |
| 补集 | Complement | 全集 − 分组 a 的成员（显式 ∪ 规则命中） |
| 全集 | Registry | 本插件已知的所有 UMO 登记簿，补集运算基准（底层直读 + 自动发现） |
| 物化 | Materialize | 把定义层解析结果写成 AstrBot 原生会话规则（sp）的过程 |
| 漂移 | Drift | 物化后，官方"自定义规则"页面等外部修改了同一 sp key，当前值 ≠ 本插件记录的物化值 |

## 2. 配置分层与继承（核心）

### 2.1 层级（优先级由低到高）

```
L0 内置默认   —— 插件自带兜底值（与 AstrBot 默认行为一致）
L1 全局默认   —— 本插件的全局 scope
L2 分组设定   —— 每个分组一份（带 priority）
L3 整群设定   —— 每个物理群一份
L4 子会话覆盖 —— 每个 UMO 一份
```

规则：
1. **只存差量**：每层只记录显式设置的字段，未设字段向下穿透。
2. **更具体优先**：L4 > L3 > L2 > L1 > L0。整群比物理群唯一、比跨群的分组更具体，故 L3 高于 L2。
3. **多分组归属（用户已确认）**：同一 priority 下一个 UMO 只能属于一个分组（store 写入时校验并拒绝）；可同时属于不同 priority 的多个分组。合并按 priority 升序，后者（高优先级）覆盖前者。同 priority 无两组同含一个 UMO，合并无歧义。
4. **规则通配**：分组规则用 fnmatch（与核心 `UmopConfigRouter._is_umo_match` 同语义）。规则命中即成员 → **新子会话自动入组、自动继承**，无需登记。两组规则可能重叠时按 (priority 升序, 组创建序) 取第一个命中并在管理页提示重叠。

### 2.2 作用域（Scope）四类字段

| 类别 | 字段 | 物化目标（AstrBot 原生 key） |
| --- | --- | --- |
| persona | persona_id / persona_source（人格源，bool，v1.0.0 新增） | `session_service_config` 的 `persona_id`（merge 写入，不整覆盖，保护 custom_name 等）；persona_source 只做解析标注与运行时拦截，不直接物化（见第 11 节） |
| provider | provider_id（chat_completion） | `provider_perf_chat_completion`（经 `provider_manager.set_provider(umo=)`，会触发变更通知） |
| service | llm_enabled / tts_enabled / session_enabled | `session_service_config` 同名字段 |
| plugins | enabled_plugins / disabled_plugins（插件名列表） | `session_plugin_config`，value 形如 `{umo: {enabled_plugins, disabled_plugins}}`（与官方 update_session_rule 同构） |

### 2.3 解析流程（resolve(umo)）

1. 提取 platform / message_type / session_id、物理群 key、UMO 是否群会话。
2. L4 子会话覆盖 → 命中字段采用（含"漂移脱离管理"的 pin 值，见第 6 节）。
3. L3 该物理群整群设定。
4. L2 所有包含该 UMO 的分组，按 priority 升序合并。
5. L1 全局默认；6. L0 内置默认。
每路带"来源层"标注输出，供生效链查看与物化决策。

## 3. 分组与集合运算

### 3.1 成员判定
- 显式成员：`members` 列表。
- 规则：`rules` 列表 fnmatch UMO 全串。
- 命中集合 = 显式 ∪ 规则命中（对全集逐个判定，或对给定 UMO 判定）。

### 3.2 全集与补集
- 全集来源（优先级）：
  1. 底层直读：`ConversationV2.user_id distinct` + umo 别名（自带"消息会话来源"列表同源；插件经 `context.get_db()` 查询，**不走 HTTP API**）；
  2. 消息事件自动发现并登记（补漏）；
  3. 手动导入（WebUI/指令粘贴 UMO）。
- **补集 = 全集 − 分组a命中集**。
- 批量必须先 dry-run：输出影响清单（可折叠）、按四类勾选、二次确认后执行、执行前快照（snapshots/，保留最近 N 次）可回滚。

### 3.3 批量语义
- 覆盖式（替换该类字段）/ 合并式（仅补缺失字段）二选一。
- 仅应用勾选的类别；未勾选类别不动。
- 对目标集合逐 UMO 重新 resolve → 物化。

## 4. 整群设定与子会话（对应"单独记忆"场景）

### 4.1 新子会话自动遵循
- **解析即继承**：新 UMO 在 L4 无记录、L3 有整群设定 → resolve 结果即整群/分组配置。
- **物化同步**：新 UMO 在首次消息到达 ProcessStage 时由本插件 handler 登记并物化（WakingCheck 早于 Process，故该 UMO **首条消息的插件过滤按默认放行，第二条起完全按配置生效**；人格/模型在同一条消息的 LLM 调用前完成物化，首条即正确——顺序：本插件 handler 设高优先级先于其他 handler 与 LLM 调用）。
- 兜底同步：插件启动、整群/分组配置变更、WebUI 打开、手动"全量同步"时对全集重算。

### 4.2 子会话可单独改动
- 查看：显示四类字段的**生效链**（每项标注来源层）。
- 改：写 L4 覆盖 → 物化；清除覆盖 → 恢复继承 → 重新物化上层结果。
- 反向同步（可选）：把某子会话的覆盖提升为整群设定。

### 4.3 场景走查（验收用例）
| # | 场景 | 期望 |
| --- | --- | --- |
| 1 | 整群 X 设人格 P；之后出现新子会话 s1 | s1 首条消息人格即为 P（物化早于 LLM 调用）；插件开关自第 2 条起准确 |
| 2 | s1 单独改人格 Q | 仅 s1=Q（L4 覆盖，pin 物化），整群仍 P；新子会话仍 P |
| 3 | 清除 s1 覆盖 | s1 恢复继承 → 重物化为 P |
| 4 | 分组 a 含群 X、Y；对"a 以外"批量设模型 M | 全集 − 命中集逐个 resolve+物化；a 内不受影响；有预览与快照 |
| 5 | 官方"自定义规则"页手改 s2 的人格为 R | 漂移检测发现 sp 值 ≠ 记录值 → s2 人格 pin 为"手改覆盖"，上层不再冲掉；管理页提示"已转手动覆盖"，可一键重新接管 |
| 6 | 同 priority 两组都想包含 s3 | store 拒绝第二次加入，提示归属冲突 |

## 5. 与 AstrBot 底层的映射（不使用 API，全部进程内底层调用）

| 本插件功能 | 底层调用（直接 import/属性访问） | 生效管线位置 |
| --- | --- | --- |
| 读人格/模型/服务/插件规则 | `sp.session_get(umo, key, default)`（`from astrbot.core import sp`） | — |
| 写人格+服务 | `sp.session_put(umo, "session_service_config", merged)`（与官方 update_session_rule 同路径；merge 保护 custom_name） | resolve_selected_persona /各 Check Stage |
| 写模型 | `context.provider_manager.set_provider(pid, ProviderType.CHAT_COMPLETION, umo=umo)` | LLM 调用 |
| 写插件开关 | `sp.session_put(umo, "session_plugin_config", {umo: {...}})` | WakingCheck：`SessionPluginManager.filter_handlers_by_session` 每消息过滤 |
| 读插件元数据 | `astrbot.core.star.star.star_map`（handler_module_path→StarMetadata：name/reserved/activated） | — |
| 会话全集 | `context.get_db()` → `select(ConversationV2.user_id).distinct()` + `get_umo_aliases()` | — |
| UMO 解析 | 自实现 split(":",2)（与 UmopConfigRouter 同构）+ 群判定看 message_type 含 "group" | — |
| 人格池/模型池（按钮选项） | `context.persona_manager.get_all_personas()`、`context.get_all_providers()` | — |
| 新 UMO 检测 | 插件 handler（`@filter.event_message_type` 等），ProcessStage 高优先级 | ProcessStage |
| 服务开关批量 | 同官方 batch_update_service 的写法（merge 各字段） | Check Stages |

注意：
- 不 import dashboard 层（SessionManagementService 是 WebUI 服务层），只复用与之相同的 **core 层** key 与语义，保证官方页面与本插件数据同构互通。
- 不 monkey-patch 核心函数；不修改 AstrBot 源码。

## 6. 同步引擎（定义层 → 物化层）

### 6.1 双层存储职责
- **定义层**（本插件 data 目录 JSON，第 7 节）：权威的"意图"——分组/优先级/规则、整群设定、子会话覆盖、全集、快照、物化记录。
- **物化层**（AstrBot 原生 sp 会话规则）：权威的"生效"——由核心管线自动消费，与官方自定义规则同构。

### 6.2 同步时机
1. 插件启动（load 后对全集 reconcile 一次，差异写入）。
2. 定义层任何变更（分组/整群/覆盖/批量执行）→ 对受影响 UMO 立即 resolve+物化。
3. 新 UMO 首条消息（ProcessStage 本插件 handler）→ 登记全集 + 物化。
4. WebUI 打开/手动"全量同步"按钮 → 全集 reconcile。

### 6.3 漂移检测与 pin（与官方页面共存的关键）
- 物化时记录 `materialized[umo][key] = 本次写入值快照`（key ∈ 四个原生键）。
- 每次同步前读当前 sp 值：等于记录值 → 视为"仍由本插件管理"，可安全重物化；不等 → **漂移**：
  - 该 (umo, key) 标记为 `pinned`（手改覆盖），其值以 sp 当前值为准（即外部改动被采纳为 L4 覆盖）；
  - 定义层上层（整群/分组/全局）不再影响该 (umo, key)；
  - 管理页显示"手动覆盖"标记，提供"重新接管"（清除 pin 并按解析重物化）。
- 反向：本插件物化后，官方页面看到的就是最新值（同 key 同构），无信息撕裂。

### 6.4 失败与并发
- 物化逐 (umo, key) try/except，单点失败记日志并进 failed 清单，不中断批次。
- 定义层写盘原子替换（临时文件 + rename），带 schema_version。
- 同步任务加 asyncio 锁，防批量与消息 handler 并发写。

## 7. 持久化（定义层）

位置：AstrBot data 目录下本插件数据目录（`get_astrbot_plugin_data_path()`），绝不存插件目录。

- `store.json` —— 主存储（schema_version、global scope、groups、clusters、session_overrides、pins、materialized 记录）
- `registry.json` —— 全集登记簿（umos、来源、发现时间）
- `snapshots/YYYYmmdd-HHMMSS-<desc>.json` —— 批量前快照（回滚用，保留最近 10 次）

JSON 单文件起步（结构简单、量级几十~几千 UMO）；若 materialized 记录过大，将该部分拆 `materialized.json` 独立。

## 8. 管理入口规划

### 8.1 群聊指令（草案，前缀默认 `/`）
| 指令意图 | 说明 |
| --- | --- |
| 分组：创建/删除/列出/设优先级 | 分组管理 |
| 分组：加入本会话/移出/加规则/移规则 | 成员维护（同 priority 冲突拒绝并提示） |
| 设置：对分组内应用四类配置 | 批量，带影响面预览 + 二次确认 |
| 设置：对分组外（补集）应用 | 一键"a 分组以外"，同上 |
| 整群：查看/设置/重置本群配置 | 整群设定 |
| 子会话：查看生效链/改/恢复继承/重新接管 | L4 粒度 |
| 同步：全量同步/状态 | 同步引擎 |
| 回滚：恢复上一次批量 | 快照 |

### 8.2 WebUI 管理页（pages/console）
- 左栏：分组树（priority 排序）+ 全集 UMO 列表（搜索、勾选、自动入组标记、pin 标记）。
- 批量栏：「应用到选中」「应用到选中之外（补集）」+ 预览弹窗。
- 右栏：作用域编辑器四个 Tab（人格/模型/服务/插件开关），按钮式选择（人格池/模型池/插件列表直接选，交互对齐官方自定义规则）。
- 子会话面板：生效链可视化（每项标注 L0-L4 来源与 pin 状态）。

### 8.3 Web API（context.register_web_api，路由前缀含插件名）
| 路由（规划） | 方法 | 用途 |
| --- | --- | --- |
| /astrbot_plugin_group_config/sessions | GET | 全集 UMO（含解析信息、pin 标记） |
| /astrbot_plugin_group_config/groups | GET/POST | 分组 CRUD、成员、规则、优先级 |
| /astrbot_plugin_group_config/scope/<layer>/<id> | GET/PUT | 读写某层 scope |
| /astrbot_plugin_group_config/resolve/<umo> | GET | 生效链（四类字段各自来源层） |
| /astrbot_plugin_group_config/batch/preview | POST | 批量（含补集）dry-run |
| /astrbot_plugin_group_config/batch/commit | POST | 批量执行（快照先行） |
| /astrbot_plugin_group_config/batch/rollback | POST | 回滚 |
| /astrbot_plugin_group_config/sync | POST | 全量同步 |
| /astrbot_plugin_group_config/pools | GET | 人格池/模型池/插件元数据（按钮选项） |

## 9. 风险与已解决清单

已解决（源码/实测确认）：
- ✅ UMO 四形态与子会话（`default:GroupMessage:本人QQ号_群号` / 裸群 / 私聊 / webchat；群号在 "_" 之后）
- ✅ 人格切换底层：`session_service_config.persona_id`（session rule 最高优先）
- ✅ 模型切换底层：`provider_manager.set_provider(umo=)`，v4 默认按会话隔离
- ✅ 插件按会话开关底层：`session_plugin_config` + WakingCheck 的 `filter_handlers_by_session`
- ✅ 全集来源：`ConversationV2.user_id distinct` + 别名
- ✅ 管线顺序与新 UMO 首条消息的生效差异（第 4.1 节，已设计兜底）
- ✅ 插件名、多分组优先级策略、管理入口（用户确认）

剩余风险：
1. 官方"自定义规则"前端字段随版本变化 → 只依赖 core 层 sp key（稳定面更大），并在启动时做 key 探测。
2. 首条消息插件过滤不生效（结构性限制）→ 文档明示 + 兜底同步。
3. materialized 记录膨胀 → 拆文件/按 key 存储。
4. `metadata.yaml` 必填字段以 helloworld 模板为准（安装调试时核对）。

## 10. 非目标（本期不做）

- 修改 AstrBot 本体或第三方插件代码（第三方插件**文件**一律不改；运行时 wrap 见第 11 节，仅拦截 persona_manager 两个人格读取方法，可整层还原）。
- 跨实例导入导出 / 云同步。
- 按用户（个人）维度配置（架构预留 L5 位置）。

## 11. 人格源（决定性人格）——v1.0.0 新增

**目标**：选择人格时把所选人格列为一切数据的源头；其他插件有生成需要时
（主动消息的人格选择、动态生活状态的人格选择等），最终拿到的是本插件的
"决定性人格"。

### 11.1 数据面：一个勾选框，四层可调

- persona 类新增字段 `persona_source`（bool），随五层继承；
  **全局 / 分组 / 整群 / 子会话 四层各一个勾选框**
  （管理页人格卡、指令 `src on|off`、`persona <名> src` 尾挂写法）。
- 语义：最终解析出 `persona_source=true` 且解析出 `persona_id` 时，该人格即
  该会话的**决定性人格**——高层可以只改人格不关开关，也可以只关开关不改人格；
  `/分组配置 me` 与 resolve 接口输出来源层与"★决定性人格"标注。
- 无会话上下文的全局生成（动态生活状态一天一份的生活状态）只认**全局层**人格源。

### 11.2 生效面：四路（modules/personality.py · PersonaSourceHub）

| 路径 | 机制 | 覆盖对象 |
| --- | --- | --- |
| ① 物化 | `session_service_config.persona_id`（SyncEngine 已有；核心 `resolve_selected_persona` 中该 key 优先级最高，force-applied） | 普通聊天与一切走核心人格解析的插件 |
| ② 对话对齐 | 同步完成后 `conversation_manager.update_conversation(persona_id=决定性人格)`（经 SyncEngine.on_synced 回调；无对话不新建） | "主动消息"这类读 `conversation.persona_id` 的插件 |
| ③ wrap `get_default_persona_v3` | 带 UMO → 该会话决定性人格；无 UMO → 全局决定性人格 | 主动消息回退路径、动态生活状态回退路径 |
| ④ wrap `get_persona` | 仅当调用方属于"人格跟随插件"（`PERSONA_FOLLOWERS`：主动消息 / 动态生活状态，按栈帧模块名识别）且**全局人格源开启**时重定向到全局决定性人格 | 动态生活状态按自己配置选人格的主路径 |

### 11.3 安全约束

- 不修改任何第三方插件**文件**；wrap 装在 `persona_manager` 单例的实例属性上，
  与核心 handler 的绑定契约无关；
- 幂等安装（`__ps_wrap__` 标记防叠层）；本插件重载时新 hub 接管 `_ps_hub` 指针，
  包装器运行时按指针路由，旧实例卸载不拆新包装；
- terminate 全量还原原始方法；任何拦截路径异常一律回落原始方法且原方法只调用一次；
- 未勾选人格源的会话/调用保持 AstrBot 原生行为（功能默认关闭，24 个单测覆盖：
  tests/test_personality.py）。
