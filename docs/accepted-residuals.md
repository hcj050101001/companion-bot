# 已接受残留与取舍清单（框架线收口版）

> P8 收口汇总，2026-10-31。本文件只汇总各阶段已经记录在案的残留、
> 取舍与未验证项，每条注明来源；不新编条目。凡来源未逐字点名的，
> 在条目内注明「代码现状可证、原文未点名」。与本文件冲突时以各
> 来源原文为准。
>
> 框架线：分支 `ai/qqbot-framework-v1`（P0–P8），tag `v0.9.0`。
> 现网仍运行多气泡线 `4d2dec0`，框架线**尚未切现网**（切换须用户
> 在场决策，检查单见 [deployment-runbook.md](deployment-runbook.md)
> §5）。多气泡线残留单列于 §4，不与框架线混记。

## 1. 框架线残留（P0–P8 与两次合并）

| # | 残留 / 取舍 | 来源 | 现状 | 用户可感知影响 |
|---|---|---|---|---|
| R1 | Codex 补审债务共 **7 笔**，均待额度恢复后以 `gpt-6.1-sol`/high 补跑：P4 `f03fc60..1af8a22`、P5 `f03fc60..2343c9a`、P4×P5 合并 `f03fc60..b2c2bdd`、P6 `b2c2bdd..9cb3a7d`、P7 `b2c2bdd..9fcdad3`、P6×P7 合并 `b2c2bdd..60f6d51`、P8 `60f6d51..<P8 收口提交>` | CHANGELOG 各阶段条目与 [0.9.0] 段汇总；runbook §5.1 列为切换前置 | 仍成立：补审清零前不得按 §5.1 切现网 | 框架线暂不上线现网，现网行为不变 |
| R2 | P4、P5、两次合并、P6、P7、P8 的阶段 Review 均为降级轮次（Codex 额度用尽，改 Claude Code + DeepSeek flash/思考 max、只读执行）；P6 第 2 轮 findings 的修复未再开第 3 轮复核，随该 diff 计入补审债务；P6 另有 2 条 P4 级 finding 以注记/文档方式处置（非代码修复） | CHANGELOG 各阶段条目 | 仍成立（与 R1 同一债务的成因） | 补审完成前，质量保障强度弱于原定强制 Codex Review 口径 |
| R3 | restore 隔离演练**未验证**：本机无 CAP_SYS_ADMIN/CAP_MKNOD、userns 内 proc/sysfs 被宿主锁定掩码阻止、debootstrap 与 systemd-nspawn 实测失败、Docker 命令不存在；替代核对（health-check 7 项 PASS、仓库版 restore `--check` 只读通过、模板逐行等价断言）不冒充演练 | CHANGELOG P7 条目；runbook §2.6 | 仍成立 | 换机恢复步骤未经真隔离演练，真换机时才第一次完整验证 |
| R4 | 回滚演练的现网部分未演练（同因无隔离环境）；仅 DB 快照回滚已在 P5 测试库演练并实测损失面 | runbook §2.6、§4.4；[memory-delivery.md](memory-delivery.md) 回滚演练记录 | 仍成立 | 现网回滚时须按 runbook §3.5/§4.3 逐步核对 |
| R5 | Docker 路径未验证：生产走 systemd，仓库 Dockerfile 路径 **unverified**（运维宿主无 Docker），不是生产路径的替代 | `deploy/README.md`；runbook §2.6 | 仍成立 | 误用 Docker 部署属未经验证路径 |
| R6 | 设备指纹固定接线已完成但**实效未验证**，不得宣称成功；恢复成功不等于指纹生效 | 方案 §3 第 9 项；runbook §2.5 | 仍成立 | 登录稳定性仍有不确定性 |
| R7 | 宿主时钟未同步、误差约 16s，用户已接受；时间工具显示值只能称「宿主时钟快照」，不引入外部时钟接口 | 方案 §3 第 5 项 | 仍成立（显式接受） | 问时间时答案可能差约 16 秒 |
| R8 | 配置热更新组件 `ConfigReloader` 已落地但运行进程尚未接线周期性重读；可热更新的只有白名单功能开关 | CHANGELOG P1 条目；[config.md](config.md) | 仍成立（未接线） | 改配置仍须重启 companion 才生效 |
| R9 | web_search 隐私防线保留直读环境校验（须覆盖任意凭证名），是密钥读取路径上一处有意例外，不全走 SecretProvider | P5 提交 `2343c9a` 信息（P1 收窄口径已由 P5 运行期接线消解，本条为例外保留） | 仍成立（刻意保留） | 无直接感知；不得表述为「密钥全部经 provider」 |
| R10 | 网络插件的域名/端点校验是对自报端点的预检而非网络沙箱；插件为同进程可信代码 | CHANGELOG P4 条目 | 仍成立（显式口径） | 插件隔离不是沙箱级，只适用于自研可信插件 |
| R11 | `tool_loop` 对 `tools.web_search.clean_synthesis` 的残留依赖：合成清洗函数尚未下沉，是 import 边界上的已知残留 | `tests/test_import_boundaries_p0.py` 文件头实测图与目标规则段 | 仍成立 | 无直接感知 |
| R12 | 长期记忆 `LongTermMemory` 只定端口、实现 deferred（Chroma 另立课题） | 方案 §5.6/§10；[state-ownership.md](state-ownership.md) | 仍成立 | 无跨会话长期记忆，只有短期近 20 轮历史 |
| R13 | 目标布局未迁完（非行为缺失）：`domain/` 仅 `claims.py`；`services/` 仅 `delivery.py`；无独立 `core/`、`ports/`、`infra/`、`apps/`、`config/` 包；迁移逻辑在 `memory/database.py` 内 | [architecture.md](architecture.md) 文末差异表 | 仍成立 | 无直接感知；找文件以 architecture 实际表为准 |
| R14 | 关怀触发节奏参数属 D2 另立阶段：P6 只接线，默认关口径不变，接线本身不产生任何发送；启用前须单独走一次 Codex 安全 Review | CHANGELOG P6 条目；方案 §7 D2 | 仍成立 | 主动关怀默认不会发任何消息 |
| R15 | D1 保守口径的已接受代价：judge 无法给出明确结论一律判疑似危机，模型故障期间误报率上升、普通聊天可能被危机话术短路 | 方案 §7 D1 | 仍成立（显式接受） | judge 故障期间正常聊天可能被固定危机话术打断 |
| R16 | 快照回滚不存在零损失：新收消息记录丢失、已执行的「忘掉」删除被撤销/已删数据可能复现、台账与 claim 回到快照时点、风格偏好与 onboarding 标记一并回退；P5 测试库演练已实测该损失面 | runbook §4.4；方案 §9 | 仍成立（显式接受） | 回滚后记录丢失与偏好重置是预期结果而非故障 |
| R17 | NapCat `good` 为硬编码 true 的盲区保留注记，不得只凭 `good=true` 宣称链路全通 | 方案 §3 第 6 项；runbook §1.2 | 仍成立 | 健康检查全绿不完全等于真实投递链路全通，以真机冒烟为准 |
| R18 | 迁移期兼容保留：`Settings.app` 保留旧字典兼容视图；旧键 `allowed_private_user_ids` 继续作正式字段、新名 `care_known_user_ids` 仅兼容读别名（并存须一致） | CHANGELOG P1 条目；[config.md](config.md) | 仍成立 | 无直接感知 |
| R19 | 时政口径冲突显式标注不静默选边：线上 persona 可作事实性中立摘要，仓库 `AGENTS.md` 仍写「禁止政治话题」，实现按线上口径 | [architecture.md](architecture.md) 开头注记；方案 §3 第 3 项 | 仍成立（文档冲突保留并注明） | 无直接感知 |
| R20 | claim 为同步短事务、不改 async（接入热路径每次 claim 一次同步 SQLite 短事务）；同口径的 CarePreferences 同步 SQLite 在事件循环内执行，外部持锁极端情况下可阻塞至其连接超时 5s（量级为按 `timeout=5` 的推算） | 代码现状可证、原文未点名：`companion/memory/claims.py` 同步 `def` + 单一 BEGIN IMMEDIATE 事务、ingress 同步调用；CHANGELOG P6 条目仅记「1 条同步 SQLite 阻塞项记为已知残留留待后续硬化」（未点名对象）；`companion/memory/care.py` 的 `sqlite3.connect(..., timeout=5)` | 仍成立 | 极端情况下事件循环短暂阻塞（关怀默认关，触发面小） |
| R21 | pipeline 容量类配置键（队列 200/50/20、claim 容量、judge 并发/等待、池 8+2）未进 Pydantic 配置树，ingress 仍经 `settings.app.get("pipeline")` 读字典 | 代码现状可证、原文未点名：`companion/config.py` 的 `AppConfig` 无 `pipeline` 字段（P8 降级 Review 第 2 轮探针实测） | 仍成立 | 生产加载路径下该组参数**不可配置**：写进 config.yaml 会被 P1 的未知键校验在启动时拒绝（fail-fast），只能用代码默认值；仅测试可经 Settings 字典直注覆盖 |
| R22 | CareSendGate 无 quota sender 的隔离记账回退分支保留 | `companion/safety/care.py` 构造注释（P5 Review #8 注记） | **生产接线已消解**（P6 已传共享 DeliveryService）；仅非生产构造的回退分支仍在 | 无直接感知 |
| R23 | §5.10 观测出口的口径限定：统一出口覆盖**工具循环诊断**（result_code 为统一结果字段）；web_search 自身的 `error_category`/`http_status` 结构化字段保留在其工具层日志，未并入 observer 出口；token 指标只统计工具循环 respond 步（普通生成与 judge 的模型调用不在口径内，指标名带 `tool_loop` 前缀明示）；「估算成本」无价格数据源、未实现，只出 token 计数 | P8 落地裁决，runbook 附录 A 已同步修订 | 仍成立（口径决定） | 成本看板不能按 /metrics 的 token 数当全量模型成本 |
| R24 | 工具诊断收割依赖单事件循环时序：Respond 在 `respond_with_tools` 返回后无 await 间隔地读 provider 客户端的 `last_tool_diagnostics`；若未来把 brain 调用多线程化，该共享可变字段会产生跨用户归因错误，须先改为返回值传递 | P8 降级 Review 结论第 3 点（核查记录） | 仍成立（当前架构下无问题） | 无直接感知 |
| R25 | `/metrics` 端点恒注册、无鉴权，部署口径为仅回环可达（companion-launch 绑定 127.0.0.1）；用户键已改为进程盐 HMAC、不可离线枚举，但任何反代/端口暴露都会放大其暴露面 | P8 降级 Review P3-4 的部分采纳处置 | 仍成立（部署假设） | 运维须保证 8090 不对公网/不可信内网暴露 |
| R26 | TransportGate 的 Stage 指标口径限定：Stage 指标只测 runner 内 Stage 耗时，TransportGate 在流水线内是命名占位；真实的验签/容量预留/claim 在 webhook 接受边界先行执行，其耗时与 401/503/重复 claim 结果不入 Stage 指标 | 本次 Codex 补审 P3 部分成立，处置为文档限定（[architecture.md](architecture.md) §5.10 可观测性段已增补） | 仍成立（口径决定） | 看 Stage 指标不得推断接受边界耗时或被拒/重复事件量，接受边界观测须另找数据源 |

## 2. 进程内状态「重启重置/丢失——显式接受」（摘要）

全文登记见 [state-ownership.md](state-ownership.md)，此处只摘
「重启即重置/丢失且已显式接受」的项：

| 状态 | 重启语义 | 影响一句话 |
|---|---|---|
| 发送额度与并发槽（`quota.SendQuota`） | 重置——显式接受 | 重启后节流短暂变松，不产生重复发送 |
| 入站限流 6/min/user、工具网关限流 | 重置——显式接受 | 重启后计数清零 |
| 危机告警去重 `crisis_history`（1h 窗口） | 重置——显式接受 | 同一危机可能多发一次告警；话术去重由落库 claim 保证 |
| 关怀阻塞时钟 `_last_crisis_at` | 重置——显式接受 | 关怀阻塞可能提前解除，仍有多重门兜底 |
| 关怀待发队列与撤回纪元 | 丢失——显式接受 | 只少发、不重发；同意与回执均落库 |
| 重复追问会话（TTL 20min）、天气缓存（<10min） | 丢失——显式接受 | 会话上下文/缓存重建 |
| onboarding 在途 claim、消费者二级去重缓存 | 丢失——可接受 | 权威状态均落库，不重复发告知、不重复处理 |
| 观测聚合 `StageMetrics`（P8 新增登记） | 重置——显式接受 | 指标进程内累计，重启清零；用户哈希盐随进程更换 |

落库项不属残留：claim、短期历史、投递台账、投递代次、人设包
选择、onboarding 标记、关怀选择与回执均为保留。

## 3. 方案 §11 未收敛点结案注记

方案对撞第 3 轮的 5 条接入层修订（v4 TransportGate 契约）未再送
Codex 复核，为方案记录的未收敛点，其正确性须由 P0 契约用例与
P3 入站饱和测试证明。P8 复核结论：**两项证明义务均已落地且现行
全绿**——

- P0 契约用例 `tests/test_adapter_contract_p0.py`：22 个测试函数、
  参数化后收集口径 26 例，文件头自述即为 §11 的 P0 proof
  obligation；原 `xfail(strict=True)` 已随 P2/P3/P5 处置全部退役
  为普通回归测试；
- P3 饱和测试 `tests/test_pipeline_saturation_p3.py`：11 例，覆盖
  单一接受边界、三道有界队列（200/50/20）、分区 claim、危机豁免、
  critical 同步兜底、worker 池预算（8+2）与 judge 等待 D1。

程序事实单独记录、不与测试结论混写：**第 3 轮修订未经 Codex
复核**这一点不因测试通过而消失，它与 R1 的 7 笔补审债务并存，
须在补审时一并复核接入层契约。

## 4. 多气泡线残留（现网已上线线，与框架线分开）

| 项 | 来源 | 现状 | 影响 |
|---|---|---|---|
| 跨分句的打字类指令失去豁免、会被拦/不拆分（按安全优先接受，不再迭代） | 定性来自多气泡第六轮收口记录；repo 内未检索到成文表述。可核查的代码事实：`repo/companion/multipart.py` 的 `_typing_context` 注释（豁免证据必须与动词同分句）与提交 `4d2dec0`（现网 HEAD）信息 | 仍成立（已上线版 `4d2dec0` 的行为） | 「逐字输入/用键盘打」说明与号码写在不同分句时不被认作打字豁免，相关内容可能按拨号指令被拦 |

## 5. N 系列残留（NapCat 全接口接入 N0–N6 收口，2026-10-31）

收口总记录见 `docs/napcat-integration-closeout.md`；以下为 N6 终审
裁决「接受为残留」的五项（编号 R-N1…R-N5），与登记表终局处置分开：
处置回答「接不接」，残留回答「接了的里面还欠什么」。

| 编号 | 项 | 裁决与理由 | 影响面 |
|---|---|---|---|
| R-N1 | 29 个动作的 return 为 bundle `p.Any`（N5 登记 28 + N6 补抽的 get_share_link）无字段级契约 | 维持三态 + 平台 data 透传。bundle 本身未定义字段，字段表只能来自实调采样；为低频动作逐个采样建约的失真风险高于收益。若某动作出现产品级字段依赖，按单动作立项采样定约 | 调用方不能依赖这些动作的返回字段结构，只能整体消费 data |
| R-N2 | `get_group_files_by_folder` 的 folder_id：bundle schema 为 Optional、网关保持必填 | 维持更严口径（已知差异）。folder 语义歧义时 fail-closed，避免误把根目录当目标文件夹 | 不传 folder_id 的调用被网关拒绝，须显式给文件夹 id |
| R-N3 | ResourceResolver 对 file:// URI 的百分号解码与平台口径可能错位 | 接受。资源面只经网关校验后透传，错位后果是平台侧拒绝（fail-closed 方向），不产生越权读取 | 含百分号编码的本地文件路径可能被平台拒收，属可用性问题非安全性问题 |
| R-N4 | signed_url 敏感标记为启发式，存在漏标面 | 接受。审计行只记资源摘要（sha256 前 16 位）不记原文，漏标不导致 URL 原文入日志；漏标的最坏后果是签名 URL 以普通资源形态被透传给平台本身 | 不影响日志泄漏面；仅影响资源分类粒度 |
| R-N5 | ResourceResolver 的 (self_id, session) 绑定/过期机制未接生产运行时 | 定位为切现网装配决策而非契约缺口：机制与网关注入点已就位且有契约测试，生产 ingress 当前走 resource_roots 口径、未注入 resolver。切现网时按 runbook §5 一并拍板是否注入 | 框架线在当前装配下不启用会话绑定；启用与否不改变网关其余行为 |

另有两项自 N4 延续的已知残留（不属 N6 新裁决，此处汇总指针）：
`.handle_quick_operation` 的上下文目标派生残留与机器人角色缓存 TTL
残留，见 N4 阶段记录与登记表对应条目 notes。

## 6. 第二段修复线封版残留（历史补审 seg2，2026-11-01）

用户 2026-11-01 20:07 封版决定接受，seg2 线以 8fabe7e 封版。以下
8 条为第二段修复线第 6 轮复审（NOT CONVERGED）留下的全部残余，
逐条在案接受、不再开新轮；如日后立项修复，以本节为清单逐条销账。

来源：审查报告
`~/workspace/agent-teams/code-review/reports/2026-11-01-170928-QQBot-seg2-fixes-r6.md`
（分支 `ai/qqbot-seg2-fixes`，基线 fa58d0e，封版 tip 8fabe7e）。

| 编号 | 项 | 裁决与理由 | 影响面 |
|---|---|---|---|
| R-S2-1 | P2：CQ 串 `file=` 引用走弱校验 `validate_resource_ref`（相对字符串原样放行），与字典形态走路径语义门不一致，`[CQ:image,file=../../etc/passwd]` 形态可相对穿越 | 封版接受。字典形态已由路径语义门拦截；字符串 CQ 形态的资源引用仍只过弱校验。**【已销账 2026-11-02】已被 seg3 集成线平价轮关闭（`c51477a`：CQ 资源参数逐个提取、同走路径语义分类，与 dict 形态同口径），随封版部署第 3 步上线** | 经 CQ 串形态构造的消息资源引用存在根目录外相对穿越面，修复需让 CQ 候选同走路径语义门 |
| R-S2-2 | P2：单斜杠 `file:` URI（`file:/etc/passwd`、`file:../secret.txt`）被 `validate_path_semantic_ref` 当作根内相对路径原样放行（scheme 识别只认 `://`） | 封版接受 | `file:` 类 scheme 前缀形态未被显式识别拒绝，单斜杠形态可绕过路径语义门 |
| R-S2-3 | P3：群回复 blocked/allowed 名单在 raw-dict 装配路径无类型守卫（标量字符串被逐字迭代、显式 null 在构造期抛裸 TypeError；pydantic 装配路径不受影响） | 封版接受。生产 config 走校验路径且现网名单为空/正常形态 | 仅手工 raw-dict 装配的异常配置形态会静默错位或构造期裸错，不影响现网运行配置 |
| R-S2-4 | P3：vision 内部后缀名单与 download 不一致，缺 `.corp/.private/.intranet/.localdomain`（后缀名单非常量共用） | 封版接受。差异仅在代理/假 IP 模式下对 vision 目标校验生效 | 代理模式下 vision 对上述内部后缀域名的拦截弱于 download 通道 |
| R-S2-5 | P3：facade schema 扫描实质只验 28/63 个动作，35 个 N4 动作在排除名单内、无字段级校验（测试证据缺口） | 封版接受。属覆盖证据不足，非运行时缺陷 | 35 个动作的 schema 一致性无自动化证据兜底，平台 schema 漂移时无测试报警 |
| R-S2-6 | P4：stream_upload/run 在已配置超时时，回调自身抛出的 TimeoutError 仍被误标 TIMED_OUT（第 1 轮只修了无截止分支） | 封版接受。仅结果标签错位，不影响成败判定 | 超时已配置时，回调超时与截止超时的审计标签不可区分 |
| R-S2-7 | P4：UTS46 快照对账测试以 `importorskip` 在 idna 缺席时静默跳过（idna 为未声明的传递依赖） | 封版接受。vendored 快照本体已钉在 `tests/uts46_ignored_snapshot.py`，对账为漂移报警 | idna 不在环境中时对账静默失效，快照漂移无报警；硬化方向为声明 dev 依赖 + 硬导入 |
| R-S2-8 | P4：新主机名形态门（`%`/尾点/空标签）只钉在首跳 URL、未钉重定向跳（测试缺口；生产逐跳复验逻辑在位） | 封版接受。属测试覆盖缺口，非行为缺陷 | 重定向跳的形态门无回归钉，相关改动需人工留意 |

## 7. 第三段修复线封版残留（历史补审 seg3，2026-11-01）

用户 2026-11-01 20:07 封版决定接受，seg3 线以 983288c 封版；其后由
集成线 `ai/qqbot-seg3-integrate` 合并部署（封版部署第 3 步，
2026-11-02 上线）。以下 8 条为第三段封版时在案的全部残余：
R-S3-1…R-S3-7 出自封版复审保留的 7 条（P3×3、P4×4），R-S3-8
为补审期已裁决的存活判定取舍。逐条在案接受、不再开新轮；如日
后立项修复，以本节为清单逐条销账。

来源：审查报告
`~/workspace/agent-teams/code-review/reports/2026-11-01-192043-seg3-fixes.md`
（分支 `ai/qqbot-seg3-fixes`，封版 tip 983288c）；集成线各轮复审
报告见同目录 `2026-11-01-203138` 起至 `2026-11-02-021014` 各份。

| 编号 | 项 | 裁决与理由 | 影响面 |
|---|---|---|---|
| R-S3-1 | P3（集成复审升 P2）：畸形 XML 回落剥标签时连属性值一并丢弃，属性藏词绕审 | **已修复（集成线关闭）**。集成线收口轮改双流并集口径：回落产出「剥标签后文本」与「原文」两流按替代关系送审（任一流命中即拦、永不拼接），属性值经原文流受审；畸形 dict 形 + CQ 形（brief=）藏词钉 + 良构孪生 + 畸形良性恰一次派发钉均在位（`af5f83f` 起，历轮复审确认） | 修复前：畸形 XML 属性值不进审查集，属性藏词可绕过内容审查 |
| R-S3-2 | P3：良构 XML 提取使字符数据重复出现（文档序流 + 原文流并集表示），跨副本接缝的回绕误拦面 | 封版接受。双流并集为 R-S3-1 修复的裁决设计（两流为替代关系、永不拼接成一条连续文本送审）；良性孪生钉在位，重复只影响审查的存在性判定 | 极端构造下良性 XML 卡片存在理论误拦面；以拆分短语漏审为更优先风险，取舍从严审侧 |
| R-S3-3 | P3：深层 XML 递归（_xml_character_data）的 RecursionError 兜底无专属测试钉 | 封版接受。属测试缺口，行为经法官探针确认正确；集成线已补深层 JSON/段形态的 fail-closed 与装配级钉，XML 递归回退的专属钉仍缺 | 该兜底回归时无测试报警；网关 invoke 层另有 RecursionError 兜底，结局同为拦截 |
| R-S3-4 | P4：claim_processing 在 claim 失败时仍无条件 INSERT gen_boot 元数据行，复活已剪除代次的元数据 | 封版接受。仅元数据账本卫生问题，不影响代次身份与收敛判定 | 已剪代次的元数据行可被失败 claim 复活，账本体积与审计噪声微增 |
| R-S3-5 | P4：forward 段子节点置于 data['messages']（非 'content'）时提取为空（法官降级：平台是否接受该段级形态无仓内证据，属 speculation） | 封版接受。'messages' 为动作级参数（send_*_forward_msg），顶层门已扫；段级形态未证实存在 | 若平台/调用方确以段级 'messages' 承载子节点，其文本不进审查集 |
| R-S3-6 | P4：资源引用门不识别 camelCase 复合键（filePath/folderPath/thumbPath）（法官降级：schema 证据仅 snake_case，属 speculation） | 封版接受。归一口径为 strip().lower()，复合驼峰键不入资源键集 | 若下游接受驼峰拼写，其下的裸本地路径不经 allowed_roots 校验 |
| R-S3-7 | P4：_payload_fallback_texts 的深度/环契约在 4 层以外无测试钉（行为经法官探针确认正确：自引用 dict 与 100 层嵌套均判不可审） | 封版接受。属测试覆盖缺口，非行为缺陷 | 该契约回归时无专属测试报警；网关结局仍由 invoke 兜底保持拦截 |
| R-S3-8 | 存活判定取舍：RequestStore 不加后台心跳/租约，失联收敛依赖 claim 时的心跳判定与 120 秒阈值清扫 | 补审期裁决接受。单进程拓扑边界已写入设计文档；多进程/多实例部署不在当前拓扑内 | 拓扑变更（多实例并行）前须先重估本条，届时需引入租约或等价机制 |

## 8. 倒计时/纪念日工具集成线残留（2026-11-01）

来源：集成复审报告 `reports/2026-11-01-122910-QQBot-countdown-integrate.md`
（分支 `ai/qqbot-countdown-integrate`，基线 fa58d0e）P3 裁决。

| 编号 | 项 | 裁决与理由 | 影响面 |
|---|---|---|---|
| R-C1 | `date_countdown` 清单查询（含「有哪些纪念日」列表模式）无主体/主人校验，任何已接入私聊用户可枚举运营者配置的全部纪念日（名称、日期、起源年份推得的周年数） | 接受为残留。列表模式是工具既定功能（PRD 口径）；私聊准入为运营者拍板的开放口径，其余只读工具（时间/天气/搜索）同样无主体门。暴露面由运营者配置内容决定：启用与填表属部署环节知情决策，`docs/config.md` 的 date_countdown 节已明示可见性。日后若需收紧，按单独立项给列表模式加主体门。**关闭注记（2026-11-01，倒计时体验增强线 `ai/qqbot-countdown-enhance`）**：已按上文预告的单独立项收紧——列表模式经宿主侧 caller_check 仅主人可用（非主人收敛为固定不可用文案、零条目回显），对话式管理命令面同口径仅主人可用且非主人不查存储。**复审修正（同日，710b2e1 复审 P2）**：可见性进一步按来源分——对话存储条目是主人私密数据，单条查询同样仅主人可见（非主人查存储独有名称与查不存在同结果，合并视图对非主人只含静态部分）；config 静态条目的按名称/日期/月日单条查询维持开放口径不变。owner 门缺省 fail-closed（未传/空主人集合时无人可列表、存储条目全不可见）。本项自该分支合入起关闭 | 运营者填入清单的纪念日对所有聊天对象可见；不得把不愿公开的私人日期填入生产清单（单条查询口径下仍成立；列表枚举面已收紧为仅主人） |
