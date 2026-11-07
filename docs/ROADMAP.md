# 实现路线（ROADMAP）

> **进度快照**：P0/P1/P2/P3/P4/P5/P6/P7 实现完成；**v1.0.0 人格源（决定性人格）完成**
> - 人格源（v1.0.0 新增）：persona_source 勾选框（全局/分组/整群/子会话四层随继承）+
>   PersonaSourceHub 四路生效（物化 / 对话人格对齐 / 两个 persona_manager wrap）+
>   指令与面板显示 ★决定性人格 —— tests/test_personality.py 24 个单测全过（全量 97 个）。
> - core 层（models/store/grouping/resolver/apply）：五层解析、补集、同优先级互斥、
>   物化同步、漂移 pin、快照回滚 —— 34 个单元测试全过；
> - main.py：Star 主类 + 底层 hooks + 新会话事件接入 + 启动对账；
> - handlers/commands.py：完整指令集；api/webapi.py + pages/console：面板管理页。
> 待做：P8（ruff 格式化、真实 AstrBot 实机联调、按反馈修整）。

## P0 需求确认与底层调研（已完成）
- [x] 插件名：`astrbot_plugin_group_config`
- [x] 子会话 sessionKey 形态：UMO 四种实测格式已记入 DESIGN 术语表（`default:GroupMessage:本人QQ号_群号` / 裸群 / 私聊 / webchat，群号在 "_" 之后）
- [x] 多分组归属策略（同优先级互斥、跨优先级按序合并）、管理入口（指令 + WebUI）
- [x] 实现方式：人格/插件切换走 AstrBot 底层逻辑、不用 API（按钮式交互）
- [x] 读源码确认全部底层入口：persona=`session_service_config.persona_id`、provider=`set_provider(umo=)`、插件开关=`session_plugin_config`+`filter_handlers_by_session`、全集=`ConversationV2 distinct`、管线顺序（见 README 第七节）

## P1 骨架跑通
- [ ] 补全 metadata.yaml 字段（对照官方 helloworld 模板）
- [ ] main.py 最小 Star 类 + 一条测试指令（如 /配置管家 状态）
- [ ] store.py：data 目录下的 JSON 读写（原子写 + 版本号）
- [ ] 在目标 AstrBot 中安装、热重载通过

## P2 数据模型与解析器（已完成）
- [x] models.py：Scope 四类差量、Group、UMO/cluster 解析、来源层标注载体
- [x] store.py：原子写、快照回滚、同优先级互斥校验、registry/materialized/pin
- [x] resolver.py：五层继承解析 + 字段级来源标注
- [x] tests/test_core.py：24 用例通过（含 DESIGN 4.3 场景 1/2/3/4/6）

## P3 分组与批量（含补集）
- [ ] grouping.py：显式成员 + 规则匹配（新会话自动入组）
- [ ] registry：会话全集登记簿（底层直读 ConversationV2 distinct + 自动发现补充）
- [ ] 批量应用：分组内 / 分组外（补集），支持 dry-run 预览、二次确认、快照回滚

## P4 整群设定与子会话（核心已完成）
- [x] 整群设定读写（store.set_cluster_scope）
- [x] 新子会话自动继承（resolve 动态解析 + sync 自动登记，测试通过）
- [x] 子会话单独覆盖 / 清除恢复继承（clear_override → 重物化）
- [x] 生效链数据（resolver 字段级 sources；展示入口待 P6/P7）
- [x] DESIGN「场景走查」自动化用例 1/2/3/4/5/6 通过（5 = 漂移 pin，见 test_apply）

## P5 三类配置落地（同步引擎已完成，业务薄封装待接）
- [x] core/apply.py SyncEngine：四类配置 → 原生物化（persona/service/provider/plugins）
- [x] 漂移检测 → 自动 pin、重新接管（takeover）、清除恢复原生默认
- [x] 批量 sync_many / 全量 reconcile_all / 单点错误隔离
- [ ] personality.py / modelcfg.py / plugin_toggle.py / cluster.py：业务薄封装（供指令与 WebUI 复用）

## P6 指令集（实现完成，待实机验证）
- [x] 分组指令（group create/join/leave/addrule/rmrule/list/scope，同优先级冲突中文提示）
- [x] 批量指令（batch group/outside 预览 + confirm/cancel，执行前自动快照）
- [x] 整群与子会话指令（cluster show/set/reset、sub show/set/reset/takeover）
- [x] 生效链查看指令（/分组配置 me）

## P7 WebUI 管理页 + Web API（实现完成，待实机联调）
- [x] pages/console：分组树 + 会话全集（搜索/组徽章/pin·覆盖标记）+ 批量页（组内/组外补集 + 预览 + 执行 + 回滚）
- [x] 配置编辑器四层（全局/分组/整群/子会话）× 四类（人格下拉/模型下拉/服务开关/插件按钮式开关）
- [x] 生效链页：字段级来源层徽章 + 原生层当前值 + 恢复继承/重新接管按钮
- [x] api/webapi.py：10 个接口 GET+POST 双注册 + <path:rest> 兜底分发（对照 ai_mind 经验）

## P8 收尾
- [ ] ruff 格式化、错误处理复查（单点错误不得拖垮插件）
- [ ] README/DESIGN 与最终实现同步
- [ ] 插件市场规范检查（目录、说明、版本）
