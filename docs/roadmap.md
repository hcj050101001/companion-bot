# 路线图（框架化 P0–P8 状态页）

> **取代关系**：本文自 P7 起取代原「四周全量（W1–W4）路线图」。
> W1–W4 计划已成历史，不再作为排期依据；其硬约束中仍然有效的
> 部分（危机方案 2、E2b 保守降级、首次告知与追问披露、密钥纪律、
> Codex 禁作生产实时 provider）已由框架方案 §3「不可退让清单」
> 逐项继承，口径以方案为准。现行路线图是部署根
> `plans/2026-10-31-qqbot-framework-architecture.md` 的 §8 分阶段
> 路线图（P0–P8），本文只同步各阶段的**实际状态**，不另立计划。
> 发布与切换操作见 [deployment-runbook.md](deployment-runbook.md)，
> 架构现状见 [architecture.md](architecture.md)。

状态口径：**已收口** = 阶段产出已落地、测试通过、阶段 Review
流程完成并提交；**进行中** = 已派发未收口；**待行** = 未开始。
版本号遵循方案 D4：迁移期只打 `v0.9.x`，真机冒烟留痕后才许
`v1.0.0`。

## 阶段状态（2026-10-31）

| 阶段 | 分支 | 状态 | 落地要点（以代码/记录为准） |
|---|---|---|---|
| P0 基线与测试网 | `ai/qqbot-framework-baseline` | 已收口 | golden 报文夹具与对话剧本测试网、已知差异登记册（`tests/KNOWN_DIFFERENCES_P0.md`，15 条）、import 边界检查骨架、CHANGELOG 与 tag 约定。纯测试资产，产品代码未动；验收时点 510 通过 / 27 预期失败（xfail 登记） |
| P1 配置与密钥形式化 | `ai/qqbot-framework-config` | 已收口 | Pydantic 配置树 `AppConfig`、fail-fast、`SecretProvider`、example 防漂移契约、热更新白名单 + 开关审计、`allowed_private_user_ids` 兼容读。见 [config.md](config.md)。549 通过 |
| P2 适配器契约 | `ai/qqbot-framework-adapter` | 已收口 | `PlatformAdapter` 协议、OneBot 适配器抽离、`Ignore(reason_code)` 全覆盖、`FakeAdapter`；P0 登记的 7 条接入差异（KD-C01～C07）全关；去重键升级为 `(platform, self_id, message_id)` 三维。613 通过 |
| P3 流水线显式化 | `ai/qqbot-framework-pipeline` | 已收口 | 11 个命名 Stage 与 Stage 0 单一接受边界、三道有界队列（200/50/20）与饱和测试、D1 保守口径落地、身份披露缺陷修复固化。提交 `f03fc60`；738 通过，仅剩 2 条预期失败留给 P5 |
| P4 工具插件化 | `ai/qqbot-framework-tools` | 已收口（与 P5 合并） | `ToolPlugin` 协议与注册表、三只读工具迁为插件、LLMProvider 能力声明替代 isinstance、工具循环显式状态机、dummy 示例进 `examples/`。提交 `1af8a22` |
| P5 记忆与投递解耦 | `ai/qqbot-framework-memory` | 已收口（与 P4 合并） | `ConversationStore`/`DeliveryLedger` 分表（同库不同表）、claim 落库、schema v0→v1 迁移、一致性契约故障注入、回滚演练（测试库）通过且损失面实测核对。提交 `2343c9a`；见 [memory-delivery.md](memory-delivery.md)、[state-ownership.md](state-ownership.md) |
| P4×P5 合并 | `ai/qqbot-framework-integration` | 已收口 | 合并提交 `ced5c6a` + 合并 Review 修复 `b2c2bdd`（本 worktree 基线）；全套 778 通过 / 0 xfailed 的合并记录见 CHANGELOG。**待办**：合并 Review 当时 Codex 额度用尽、降级为 Claude Code 复核，Codex 补审未清零——计入 P8 前置条件（runbook §5.1） |
| P6 调度与关怀接线 | `ai/qqbot-framework-scheduler` | 已收口 | Scheduler 接线、连接健康迁为调度任务、同意控制面补齐（opt-in/pause/stop/forget、「忘掉」覆盖关怀数据、发送前实时重查）、撤回时序剧本、登记册唯一未关闭条目 KD-S03 已关闭（登记册 15 条全部关闭）。提交 `9cb3a7d`；`proactive_care.enabled` 仍为 false（生产默认关口径不变，触发参数 D2 另立阶段） |
| P7 部署资产与发布规范 | `ai/qqbot-framework-ops` | 已收口 | `deploy/` 入仓（systemd 模板、runtime、bin；已在本 worktree 落地）、Dockerfile（healthcheck + 非 root）、发布 runbook 与架构/路线图文档重写（本文与 [architecture.md](architecture.md)、[deployment-runbook.md](deployment-runbook.md)）、examples 补齐。提交 `9fcdad3`。restore 隔离演练结论已回填 runbook §2.6：本机无可用隔离环境（能力集与 proc/sysfs 挂载受限，实测证据在 §2.6），记**未验证**；P7 Review 为 Codex 额度降级 Claude Code 执行，Codex 补审待补（计入 P8 前置条件） |
| P6×P7 合并 | `ai/qqbot-framework-integration2` | 已收口（本 worktree） | P6（`9cb3a7d`）与 P7（`9fcdad3`）同基线 `b2c2bdd` 并行分叉后合流；唯一内容冲突为 `docs/architecture.md`（P7 全量重写为准、P6 的 scheduler 已接线事实回填其包结构行与差异表，旧 W1 模块表随重写退役），其余自动合并。合并 Review 为 Codex 额度降级 Claude Code 执行；Codex 补审债务至此共 6 笔（清单见 CHANGELOG），计入 P8 前置条件（runbook §5.1） |
| P8 框架 1.0 收口 | `ai/qqbot-framework-v1` | 已收口（框架侧） | §5.10 工具诊断统一观测出口落地（pipeline observer 汇出 + `/metrics` 端点）、登记册 15 条逐条复核全关闭、§11 未收敛点以 P0 契约用例 + P3 饱和测试全绿结案、全局残留清单成文（[accepted-residuals.md](accepted-residuals.md)）、文档收口、全量终验全过。打 **`v0.9.0`**（本地 tag）：真机冒烟未留痕（须用户或值守执行），不许打 `v1.0.0`；**现网未切**——切换与冒烟须用户在场决策，前置含 Codex 补审 7 笔清零（见 runbook §5.1 与残留清单 R1） |

阶段间依赖（方案 §8）：P0→P1→P2→P3→（P4/P5 并行）→P6 依赖
P3/P5；P7 可与 P4 起并行；P8 收口。每阶段独立分支 + 独立
worktree、Codex Review 强制、现网全程不动；每阶段回滚点 = 该
阶段分支的基线 commit（P5 起数据库回滚另按 runbook §4）。

## NapCat 全接口接入（N0–N6，2026-10-31 全部收口）

在框架线之上把本机 NapCat 4.18.41 的动作面全量接入网关契约，
方案 `plans/2026-10-31-napcat-full-api-integration.md`，收口总
记录 [napcat-integration-closeout.md](napcat-integration-closeout.md)。

| 阶段 | 分支 | 状态 | 落地要点 |
|---|---|---|---|
| N0 验证与登记固化 | `ai/qqbot-napcat-n0` | 已收口 | 191 条登记表冻结为数据 + 三方对账全绿；零运行时变更 |
| N1 传输与网关收敛 | `ai/qqbot-napcat-n1` | 已收口 | OneBotTransport 单一出口 + ActionGateway 单一收口 + 8 门面；L0 62 条接通；L3 硬禁生效 |
| N2 消息形态 | `ai/qqbot-napcat-n2` | 已收口 | SendTarget + 两级额度（C8）；21/23 段类型可发；接通 93 |
| N3 入站事件面 | `ai/qqbot-napcat-n3` | 已收口 | 四型判别联合 + 群只观察子链 + request 处置状态机；接通 97 |
| N4 群场景与 L2 | `ai/qqbot-napcat-n4` | 已收口 | L2 授权链 + 确认令牌 + 群回复通路（开关默认关）；接通 132。Review 自本阶段起按用户指示改 Claude Code（Codex 额度耗尽） |
| N5 文件媒体域 | `ai/qqbot-napcat-n5` | 已收口 | bundle schema 证据定稿 40 动作；ResourceResolver/FileTaskManager/相册熔断；接通 160 |
| N6 敏感面与收口 | `ai/qqbot-napcat-n6` | 已收口 | L3 受控解锁（默认硬禁不变）；登记表终审两条改判 excluded_internal；**接通 167 = 全部 canonical 在册条目**；收口文档与开关一览成文 |

N 系列与框架线一样**未切现网**：切换仍按 runbook §5、由用户
单独决策（前置含真机冒烟留痕）。

## 与现网的关系（不变口径）

- 框架线与现网是两条线：现网运行多气泡线版本（部署根 `repo/`
  磁盘 HEAD 与运行版本见各次发布的 MANIFEST，两者分开记录），
  框架线成果只在各 worktree/`ai/` 分支内，**尚未切现网**。
- 切换只发生在 P8、单独开部署窗口，前置条件与检查单见
  [deployment-runbook.md](deployment-runbook.md) §5。
- 迁移期现网保持运行、保持现版本；框架阶段不碰生产 config、
  不重启服务、不发真实 QQ 消息。

## 历史：W1–W4 四周计划（已归档）

原四周计划（W1 骨架与安全网关、W2 长期记忆与感知、W3 主动
关怀与表达、W4 社交表面与运维）已由框架方案吸收或明确不做
（方案 §10：不做长期记忆 Chroma 实现只留端口、不做群聊回复、
不做多媒体理解、不接 SaaS 观测等）。其中「不做群聊回复」一项
已由 NapCat 全接口接入 N4 推翻：回复通路已落地（产品开关
`tools.group_chat.reply_enabled` 默认关、无生产启用），群内
产品行为仍未定义，见 NapCat 接入方案 §6 N4。W2 的 Chroma、W4 的运维
面板等未落地项不再自动续期，需要时另立阶段。历史验收材料
原内部验收文件（`tests/W1-*.md`）已在开源清洗中移除，验收口径摘要保留在 [e2b-acceptance.md](e2b-acceptance.md)
等文档中，仅作追溯，不作排期。
