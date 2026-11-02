# P0 已知差异登记册（Known Differences）

基线：`151fff5`（框架化 P0 阶段）。本册汇总 P0 验收中「契约要求 vs 现行行为」
的全部已登记差异：KD-001、KD-C01～KD-C11、KD-S01～KD-S03，共 **15 条**。

## 总则

- **表示法**：可测差异一律以 `xfail(strict=True)` 用例登记在案，理由串以
  差异编号开头。用例断言的是**契约行为**，不是现行行为；现行行为不满足
  断言即 xfail，属预期状态，不是测试失败。
- **转 pass 的处置**：任一 xfail 用例在后续阶段被修复而转为 XPASS 时，
  strict 模式会把它报为硬失败——这是故意的信号。处置人必须在同一变更中
  回到本册：把该条状态改为「已关闭」，注明关闭阶段与提交，并把用例的
  xfail 标记去掉（或改写为契约回归用例）。不得以放宽 strict、删除断言
  或跳过用例的方式「消音」。
- **带入禁令**：未处置（未修复且未在本册改判延期/关闭）的差异不得带入
  P8；P8 验收前须逐条核对本册，状态不为「已关闭/已改判」的条目即阻塞。
- **新增差异**：后续阶段发现新的契约差异，按本册格式续编（对话/语义类
  KD-0xx、契约/传输类 KD-Cxx、梳理类 KD-Sxx），先登记、后排期，不得直接
  改测试迁就现行行为。
- 无 ingress 可测面的差异以「文档级已知差异」单列（见 KD-S03），注明
  无 xfail 用例的原因；一旦出现可测面，必须补 xfail 用例再进入处置阶段。

## 登记条目

### KD-001 身份披露检查弱于契约（§3.3 身份三项）

- **差异描述**：身份披露契约要求身份类回答答全三项——名字/身份
  （阿年 / AI 陪伴助手）、模型显示名（DeepSeek）、对话由第三方模型处理。
  现行披露检查在普通（多气泡）路由与工具路由中都只要求出现泛化 AI 词，
  只答「我是 AI 助手」即过检；且身份问题匹配覆盖不全，问法
  「你背后用的是什么模型」不触发披露检查，模型答「这是秘密」被原样发出。
- **实测证据**：`tests/test_dialogue_scripts_p0.py` 三条参数
  （剧本在 `tests/scripts_p0/02_identity.yaml`；P3 前为 strict xfail，
  P3 回册时已去掉 kd 标记、转为常规回归）：
  `identity_ordinary_weak_ai_assistant`（普通路由同形态）、
  `identity_tool_weak_ai_assistant`（工具路由同形态）、
  `identity_tool_uncovered_question_secret`（未覆盖问法 +「这是秘密」）。
  对照通过项：否认 AI 身份时回退固定披露文案的
  `identity_ordinary_denial_fallback` 为普通 pass，说明回退文案本身合规、
  弱的是检查与匹配。
- **影响**：用户问身份时可能得到不完整披露，与「如实 AI 身份」底线及
  第三方模型处理告知要求有差距；未覆盖问法下甚至零披露。
- **处置阶段**：P3。**处置方式**：修复（披露检查改为三项齐备校验 +
  扩身份问法匹配）。**状态**：已关闭（关闭阶段：P3；分支 ai/qqbot-framework-pipeline）。

### KD-C01 无 message_id 的私聊仍被完整处理

- **差异描述**：v4 §5.3 步骤 2 要求无键（无 message_id）事件直接
  Ignore、不建 claim；现行对无 message_id 私聊完整处理并回复，重复
  到达重复回复，无去重可能。
- **实测证据**：`tests/test_adapter_contract_p0.py::
  test_kd_c01_message_without_message_id_is_ignored`（strict xfail）；
  黄金夹具 `tests/fixtures/golden/private_no_message_id.json`
  （`tests/test_golden_payloads.py` strict xfail）。
- **影响**：无键事件可穿透去重，重复投递即重复回复；与单一接受边界
  的 claim 语义不一致。
- **处置阶段**：P2。**处置方式**：修复（键提取与无键 Ignore）。
  **状态**：已关闭（关闭阶段：P2；分支 ai/qqbot-framework-adapter；提交见本分支 tip）。

### KD-C02 self_id 从未被读取或要求

- **差异描述**：v4 §5.3 步骤 2 要求 self_id 与 message_id 俱在才可
  claim；现行代码从不读取 self_id，缺失时照常处理并回复。
- **实测证据**：`test_kd_c02_message_without_self_id_is_ignored`
  （strict xfail）；黄金夹具 `private_no_self_id.json`（strict xfail）。
- **影响**：多账号/多实例下无法按机器人账号隔离事件与去重键（与
  KD-C03 相连）。
- **处置阶段**：P2。**处置方式**：修复。**状态**：已关闭（关闭阶段：P2；分支 ai/qqbot-framework-adapter；提交见本分支 tip）。

### KD-C03 去重键仅 message_id 单维

- **差异描述**：v4 claim 键为 `(platform, self_id, message_id)`；现行
  去重键仅 message_id，同 id 不同 self/用户的第二条被误吞。
- **实测证据**：`test_kd_c03_dedup_key_includes_self_id`（strict
  xfail，同 id 两 self 各应回复一次、现行只回复一次）。
- **影响**：跨账号/跨用户 id 碰撞时合法消息被静默丢弃。
- **处置阶段**：P2/P3（P2 键提取先行、P3 随 claim 落库定型）。
  **处置方式**：修复。**状态**：已关闭（关闭阶段：P2；分支 ai/qqbot-framework-adapter；提交见本分支 tip）；claim 落库不在本阶段（属 KD-C08，P3/P5 继续登记）。

### KD-C04 不合格事件在资格判定前建 claim（占位毒化）

- **差异描述**：现行在解析/资格判定前把所有带 message_id 的报文写入
  `accepted_ids`，群聊、残缺报文等不合格事件先占位，同 id 私聊随后被
  去重吞掉；v4 要求不合格事件不建 claim、禁用占位键。
- **实测证据**：`test_kd_c04_group_event_does_not_claim_message_id`、
  `test_kd_c04_malformed_dict_does_not_claim_message_id`（均 strict
  xfail）；黄金夹具 `group_message_id_poisons_private.json`、
  `malformed_bare_dict_with_message_id.json`（均 strict xfail）。
- **影响**：任意不合格报文可凭 id 毒化后续合法私聊，造成可被外部
  触发的消息丢失。
- **处置阶段**：P2。**处置方式**：修复（claim 时机后移至资格判定
  之后）。**状态**：已关闭（关闭阶段：P2；分支 ai/qqbot-framework-adapter；提交见本分支 tip）。

### KD-C05 Ignore 无 reason_code 与事件类型摘要

- **差异描述**：现行 Ignore 只记 `event_type=ignored`，无 `reason_code`
  与事件类型摘要，排障无据（§2.1 痛点 5）；v4 §5.2 要求
  `Ignore{reason_code, event_type_summary}`。
- **实测证据**：`test_kd_c05_ignore_log_carries_reason_code_and_type_summary`
  （strict xfail，断言日志末条含 `reason_code="not_private"` 与摘要）。
- **影响**：被忽略事件无法按原因归类统计，线上排障只能猜。
- **处置阶段**：P2。**处置方式**：修复。**状态**：已关闭（关闭阶段：P2；分支 ai/qqbot-framework-adapter；提交见本分支 tip）。

### KD-C06 畸形报文返回 400 而非 Ignore 路径

- **差异描述**：非 JSON、非对象 JSON（数组/标量）报文现行返回 400；
  v4 单一接受边界内畸形应走 Ignore(malformed) 终止（204）并记
  reason_code。
- **实测证据**：`test_kd_c06_non_json_body_uses_ignore_path`、
  `test_kd_c06_non_object_json_uses_ignore_path`（均 strict xfail）；
  黄金夹具 `malformed_non_json_body.json`、
  `malformed_json_array_body.json`、`malformed_json_scalar_body.json`
  （均 strict xfail）。
- **影响**：畸形报文的处置路径与状态码和契约不一致，上游/监控按
  400 解读会误判为客户端错误而非已忽略事件。
- **处置阶段**：P2。**处置方式**：修复。**状态**：已关闭（关闭阶段：P2；分支 ai/qqbot-framework-adapter；提交见本分支 tip）。

### KD-C07 缺 user_id 未在 Normalize 被拒

- **差异描述**：缺 user_id 的私聊未在 Normalize 被拒，`parse_event`
  产出空 user_id 仍进处理链（对空用户尝试发送、污染限流/记忆键）；
  v4 应 Ignore(malformed)。
- **实测证据**：`test_kd_c07_message_without_user_id_is_ignored_at_dispatch`
  （strict xfail）；黄金夹具 `private_no_user_id.json`（strict xfail）。
- **影响**：空身份事件污染限流与记忆键空间，并产生无意义发送尝试。
- **处置阶段**：P2。**处置方式**：修复。**状态**：已关闭（关闭阶段：P2；分支 ai/qqbot-framework-adapter；提交见本分支 tip）。

### KD-C08 去重/claim 状态进程内、无界、不持久

- **差异描述**：去重/claim 为进程内集合（`accepted_ids` /
  `seen_message_ids` 无界、仅粗暴清空），重启即丢、事件被完整再处理；
  危机路径不经投递台账，重启后重复危机报文被再次处理并重发固定
  话术（并可能重复告警）。v4/D3 要求落库 claim、终态保留 24h、带
  代次号。普通回复的重发目前仅被投递台账 turn_id 守卫偶然掩盖，
  不等于 claim 持久化成立。
- **实测证据**：`test_kd_c08_dedup_survives_restart`、
  `test_kd_c08_crisis_duplicate_is_not_resent_after_restart`
  （原均 strict xfail，以同设置第二 app 实例模拟重启；P5 落库后
  已去掉 xfail 标记、转为常规回归用例并实测通过）。另有
  `tests/test_memory_p5.py` 的 claim 跨重启用例直接验证
  SqliteClaimStore 两代实例间的 DUPLICATE / abandoned 不重放 /
  EXEMPT 计数落库累计。
- **影响**：每次重启/换机后的重复投递会造成重复回复、重复危机
  话术与重复告警；无界集合另有内存增长面。
- **处置阶段**：P3（契约）/ P5（落库）。**处置方式**：修复。
  **状态**：已关闭（关闭阶段：P5；分支 ai/qqbot-framework-memory；
  提交见本分支 tip）。落库形态：`SqliteClaimStore` 与会话/台账同库
  （memory.db 的 claims 表 + schema_meta 中的代次/豁免计数），
  P3 定型的键/分区/容量/代次/终态 24h 淘汰语义不变；构造时旧代次
  processing 一律转 abandoned 不重放。进程内的 `seen_message_ids`
  粗粒度集合仍在消费者收尾路径作二级缓存（>10000 清空），其丢失
  不再影响正确性——权威去重以落库 claim 为准。

### KD-C09 无按道分区的 claim 容量

- **差异描述**：无「普通分区硬上限 + critical/control 保留分区」的
  claim 容量机制；v4 §5.3 Stage 0 要求分区保底，普通流量挤不占
  保留分区。现行 app 上不存在 `claim_store` 分区结构。
- **实测证据**：`test_kd_c09_claim_capacity_is_partitioned`
  （P3 前为 strict xfail，P3 回册时已去掉标记、转为常规回归；断言
  `claim_store.partitions ⊇ {ordinary, reserved}`）。
  本探针仅验结构前置，行为级饱和验证（普通流量挤占时危机保底仍成立）为 P3 饱和测试强制项
  （已由 `tests/test_pipeline_saturation_p3.py` 覆盖）。
- **影响**：流量洪峰下普通事件可耗尽全部接受容量，危机/控制事件
  无保底通道。
- **处置阶段**：P3。**处置方式**：修复。**状态**：已关闭（关闭阶段：P3；分支 ai/qqbot-framework-pipeline）。

### KD-C10 入站队列无界、无 503 单一接受边界

- **差异描述**：`EventBus` 为无界单队列（`maxsize=0`）、永不返回
  503、无三道（ordinary/critical/control）与容量预留；v4 要求
  「容量预留 → claim → 204」单一接受边界，普通/control 满时在建
  claim 前 503。
- **实测证据**：`test_kd_c10_inbound_queue_is_bounded`
  （P3 前为 strict xfail，P3 回册时已去掉标记、转为常规回归）。
  该用例已经 Codex 复审改为行为级探针（不再只验 `maxsize` 属性），
  复审第 2 轮残留修复后改为自注入小容量：消费者未启动（无人排空
  队列）时，先把 `app.state.event_bus.queue` 替换为 `maxsize=3` 的
  有界队列（`EventBus.publish` 的 `queue.put` 即走此队列），连发
  capacity+1=4 条不同 id 的私聊，断言应在有限条数内出现 503、每条
  POST 设 2 秒显式截止、超时视为「被阻塞而非 503」断言失败。
  `--runxfail` 探针实测：前 3 条 204 填满注入队列，第 4 条在 publish
  上阻塞、命中超时断言而失败（用例约 3 秒，未吃满 10 秒超时），
  xfail 精确落在目标行为上。此前连发 100 条的写法若未来实现容量
  恰为 100 将永远测不出 503（假登记），故废弃。
- **影响**：背压不存在，上游洪峰直接转成内存与处理积压；过载
  语义对上游不可见。
- **处置阶段**：P3。**处置方式**：修复。**状态**：已关闭（关闭阶段：P3；分支 ai/qqbot-framework-pipeline）。

### KD-C11 无关键词危机 claim 豁免与 critical 专职执行池

- **差异描述**：无「关键词危机豁免 claim 直进 critical 道、critical
  专职执行池与同步最小危机响应兜底」机制；v4 §5.3 要求保留分区
  满时仍保底危机。现行 app 上不存在 `critical_executor`。
- **实测证据**：`test_kd_c11_crisis_lane_mechanism_exists`
  （P3 前为 strict xfail，P3 回册时已去掉标记、转为常规回归；断言
  `app.state.critical_executor` 存在）。
  本探针仅验结构前置，行为级饱和验证（普通流量挤占时危机保底仍成立）为 P3 饱和测试强制项
  （已由 `tests/test_pipeline_saturation_p3.py` 覆盖）。
- **影响**：拥塞时危机消息与普通消息同道排队，固定危机话术的
  时效性无机制保证（现行靠消费者不等待普通任务的调度偶然性）。
- **处置阶段**：P3。**处置方式**：修复。**状态**：已关闭（关闭阶段：P3；分支 ai/qqbot-framework-pipeline）。

### KD-S01 judge 普通异常被放行（非保守降级）

- **差异描述**：`SafetyGateway.detect`（`companion/safety/__init__.py`）
  对 `JudgeUnavailable` 按 E2b 保守降级判「疑似危机」，但对其他
  普通异常走 `except Exception: result = None` 放行，普通回复照常
  发送。这与 D1 保守降级口径不一致：现行生产 judge
  （`ModelLLMJudge`）会把自身失败统一转成 `JudgeUnavailable`，故
  该缺口在默认装配下被掩盖；任何直接实现 `LLMJudge` 接口、抛普通
  异常的 judge（或未来替换 judge 实现）都会静默失去保守降级。
- **实测证据**：梳理实证探针（P0 登记时实测）：直调
  `SafetyGateway([], ExplodingJudge).detect(...)` 返回 `(False, "")`；
  集成路径下普通回复「测试回复」被发送而非固定危机话术。登记用例
  `tests/test_known_differences_p0.py::
  test_kd_s01_ordinary_judge_exception_is_suspected_crisis`
  （P3 前为 strict xfail，P3 回册时已去掉标记、转为常规回归；
  断言应发固定危机话术）。
- **影响**：安全网关的失败语义依赖 judge 实现细节，换 judge 即
  可能把危机漏检从「疑似危机」降级为「正常放行」。
- **处置阶段**：P3。**处置方式**：修复（普通异常与
  JudgeUnavailable 同口径：判疑似危机；或从接口层强制 judge 只抛
  JudgeUnavailable 并在网关对违约异常保守处理）。
  **状态**：已关闭（关闭阶段：P3；分支 ai/qqbot-framework-pipeline）。

### KD-S02 含糊时间澄清问答被写入短期记忆

- **差异描述**：current_time 直达路以 `tools_used=True` 经
  `_deliver(store=not tools_used)` **不写**短期记忆（既有测试已断言
  直达后历史为空）；但含糊问法分支（`companion/ingress/__init__.py`）
  回 `CLARIFY_REPLY` 时置 `tools_used=False`，同一 `_deliver` 据此
  `store=True`，把澄清问答写入短期记忆。两条同属时间工具的路径
  记忆语义不一致。
- **实测证据**：梳理实证探针（P0 登记时实测）：已 onboarding 用户
  发「几点」后，回复确为 `CLARIFY_REPLY`，且
  `memory.get_history` 为 `[('user', '几点'), ('assistant', 澄清文案)]`
  非空。登记用例 `tests/test_known_differences_p0.py::
  test_kd_s02_ambiguous_time_clarify_writes_no_memory`
  （P3 前为 strict xfail，P3 回册时已去掉标记、转为常规回归；
  先断言确已走澄清分支、再断言历史应为空）。
- **影响**：澄清问答进入对话历史，污染后续模型上下文，并与
  「工具直达不落记忆」的既定语义冲突；用户若随后「忘掉」虽可清除，
  但默认行为不一致本身即缺陷。
- **处置阶段**：P3。**处置方式**：修复（澄清分支与直达路同口径
  不写记忆，或在登记册改判为两路一致写记忆并同步直达路——二选一
  须在 P3 定版，不得维持现状不一致）。
  **状态**：已关闭（关闭阶段：P3；分支 ai/qqbot-framework-pipeline）。

### KD-S03 私聊「忘掉」不覆盖关怀数据（P0 登记时为文档级；P6 已关闭）

- **差异描述**：私聊「忘掉」分支（`companion/ingress/__init__.py`）
  只清会话记忆（`ConversationStore.forget`，P5 前为
  `ShortTermMemory.forget`）与重复追踪
  （`repetition.forget`）；`CarePreferences` 虽有 `forget/stop`
  API，但从未接线到 ingress——关怀同意/偏好数据不在「忘掉」范围。
  这与 §3.8「忘掉须覆盖该用户的可删数据」底线的目标态有差距；
  `docs/care-consent.md` 亦明文该切片「does not change the existing
  short-term memory forget path; connecting both is future
  integration work」。
- **实测证据**：代码核查（P0 登记时）：ingress 全文无
  `CarePreferences` 引用；忘掉分支仅上述两处清理；关怀存储为独立
  仓外 SQLite（`companion/memory/care.py`），且现行生产装配中
  主动关怀未接线开启，故当前无实际关怀数据被「忘掉」遗漏——差异
  在控制面接线层面成立、在数据层面尚未兑现。
- **无 xfail 用例的原因**：ingress 无关怀偏好可测面（无实例、无
  命令路径），任何用例只能断言「对象不存在」式的牵强命题，不构成
  契约证据；按总则以文档级登记，待 P6 出现可测面时先补 xfail
  用例再处置。
- **影响**：P6 关怀同意控制面接线后，若忘掉范围不同步扩围，用户
  行使删除权时关怀数据会被遗漏，届时由文档级差异升级为实际
  合规缺口。
- **处置阶段**：P6（同意控制面事项）。**处置方式**：修复——P6 接线
  时把 `CarePreferences.forget/stop` 纳入统一忘掉路径，并补可测用例。
  **状态**：已关闭（关闭阶段：P6；分支 ai/qqbot-framework-scheduler）。
  关闭实测：关怀同意控制面已接线（Admission 前置分支的
  opt-in/pause/stop 命令 + CareScheduler 待发队列），「忘掉」在同一
  `_state_locks` 临界区内依次清会话历史/台账（ConversationStore
  .forget）、重复追踪与关怀同意（CarePreferences.forget），并使该
  用户待发关怀失效，确认回复在全部持久化完成后才发出；关怀存储为
  仓外独立 SQLite、与会话库无法同事务，原子性由该临界区串行保证
  （既定口径）。可测面已随接线出现，回归用例见
  `tests/test_care_wiring_p6.py`（忘掉覆盖关怀 + 排队关怀失效、
  撤回保底、命令流、未接线等价）。

## 汇总

| 编号 | 一句话 | 处置阶段 | 方式 | 状态 |
|---|---|---|---|---|
| KD-001 | 身份披露检查只认泛化 AI 词、三项不全亦过检，未覆盖问法零披露 | P3 | 修复 | 已关闭（P3） |
| KD-C01 | 无 message_id 私聊仍被处理回复 | P2 | 修复 | 已关闭（P2） |
| KD-C02 | self_id 从未被读取或要求 | P2 | 修复 | 已关闭（P2） |
| KD-C03 | 去重键仅 message_id 单维 | P2/P3 | 修复 | 已关闭（P2） |
| KD-C04 | 不合格事件资格判定前建 claim 毒化同 id 私聊 | P2 | 修复 | 已关闭（P2） |
| KD-C05 | Ignore 无 reason_code 与类型摘要 | P2 | 修复 | 已关闭（P2） |
| KD-C06 | 畸形报文返回 400 而非 Ignore 路径 | P2 | 修复 | 已关闭（P2） |
| KD-C07 | 缺 user_id 未在 Normalize 被拒 | P2 | 修复 | 已关闭（P2） |
| KD-C08 | claim 进程内、无界、不持久，重启重复处理/重发危机 | P3 契约/P5 落库 | 修复 | 已关闭（P5） |
| KD-C09 | 无按道分区的 claim 容量 | P3 | 修复 | 已关闭（P3） |
| KD-C10 | 入站队列无界、无 503 单一接受边界 | P3 | 修复 | 已关闭（P3） |
| KD-C11 | 无危机 claim 豁免与 critical 专职执行池 | P3 | 修复 | 已关闭（P3） |
| KD-S01 | judge 普通异常被放行、非保守降级 | P3 | 修复 | 已关闭（P3） |
| KD-S02 | 含糊时间澄清问答被写入短期记忆 | P3 | 修复 | 已关闭（P3） |
| KD-S03 | 「忘掉」不覆盖关怀数据（未接线） | P6 | 修复 | 已关闭（P6） |

xfail 用例合计 27 条，现已全部转正：其中 P2 已关闭 KD-C01～KD-C07
对应 17 条（C01×2、C02×2、C03×1、C04×4、C05×1、C06×5、C07×2），
其 xfail 标记已去掉、转为常规回归用例；P3 已关闭 KD-001、KD-C09、
KD-C10、KD-C11、KD-S01、KD-S02 对应 8 条（KD-001×3、KD-C09×1、
KD-C10×1、KD-C11×1、KD-S01×1、KD-S02×1），其 xfail/KD 标记已去掉、
转为常规回归用例；P5 已关闭 KD-C08 对应最后 2 条（落库持久化，
分布于 `test_adapter_contract_p0.py`），xfail 标记已去掉、转为常规
回归用例。至此登记册内可测差异的 xfail 已清零（全套 0 xfailed）；
KD-S03 为文档级差异（无 xfail 用例），已于 P6 随关怀同意控制面
接线处置关闭（回归用例见 `tests/test_care_wiring_p6.py`）。**至此
本册 15 条已知差异全部关闭**，P8 验收前置条件（无未处置条目带入）
已满足。

---

## P8 收口复核与 §11 结案注记（2026-10-31）

P8 收口时逐条复核本册：15 条已知差异状态均为「已关闭」（见上表
与各条正文），无「已登记未处置」条目带入 P8，P8 验收前置条件
满足。汇总口径复核：27 条 xfail 已全部转正（P2 关闭 17 条、
P3 关闭 8 条、P5 关闭 2 条），KD-S03 为文档级差异、已于 P6
关闭，全套 0 xfailed（框架线终验数字见 CHANGELOG [0.9.0] 段）。

§11 未收敛点结案：方案对撞第 3 轮的 v4 接入层（TransportGate）
契约未再送 Codex 复核，其证明义务为本册配套的 P0 契约用例与
P3 入站饱和测试。两项均已落地且现行全绿：契约用例
`tests/test_adapter_contract_p0.py`（22 个测试函数、参数化收集
26 例，文件头自述为 §11 的 P0 proof obligation）、饱和测试
`tests/test_pipeline_saturation_p3.py`（11 例）。据此 §11 未收敛
点以测试证据结案；「未经 Codex 复核」的程序事实与 Codex 补审
债务（7 笔，见 `docs/accepted-residuals.md` R1）分开记录，补审
时须一并复核接入层契约。
