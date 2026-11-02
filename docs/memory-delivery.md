# 记忆与投递（P5 落地记录）

方案 §5.6 的实现说明：存储拆分、一致性契约、迁移与回滚规程。
现网切换与数据库备份/回滚的通用规程以方案 §9 为准，本文件只记
本阶段的实现与演练结论；生产库不在本阶段动过。

## 存储拆分（同库不同表）

memory.db（路径 `memory.sqlite_path`，默认 `data/memory.db`）自
schema v1 起由三组存储共享一个 `Database`（WAL + 单连接串行化 +
显式事务）：

- `ConversationStore`（`companion/memory/conversation.py`）：短期
  历史 `messages`（近 20 轮）、`user_state`（onboarded）、
  `pack_preferences`（人设包选择与代次）。接口异步化（async +
  `asyncio.to_thread` 事务核心），消除 async 链路同步阻塞。
- `DeliveryLedger`（`companion/memory/ledger.py`）：投递状态机
  `delivery_turns`（状态 prepared/in_flight/partial/complete/
  ambiguous/failed、unsent 计数、已接受文本）与
  `delivery_generations`。
- `SqliteClaimStore`（`companion/memory/claims.py`）：入站去重
  claim 落库（`claims` 表），语义与 P3 定型一致。

`messages` 只存**历史投影**：user 行在 prepare 时与台账行同事务
写入；assistant 行只在 partial/complete 时由已接受气泡物化/更新。
长期记忆仍为 deferred 端口（`LongTermMemory` 只定接口）。

## 一致性契约（§5.6）

- 台账状态迁移与历史投影在**同一个 SQLite 事务**内完成
  （prepare / update_delivery / forget 都是单一 BEGIN IMMEDIATE
  事务），不存在「台账已记、历史未写」的中间态落盘。
  `Database.fault_hook` 在事务内「台账已写、投影未写」点提供故障
  注入，测试证明该点崩溃时两边都不落盘
  （`tests/test_memory_p5.py`）。
- 删除（忘掉）走同一事务并以 generation 围栏校验：清历史、清台账、
  两个 generation 同时 +1；投递在途时忘掉，迟到的 update 因围栏
  不符或回合不存在而被拒（同上测试）。
- **ambiguous 回合的已接受前缀不进历史**：转 ambiguous/failed 时
  在同一事务内删除该回合的 assistant 投影行；已接受文本仍留在
  台账行内备查，但不出现在 get_history。
- 未发送/未接受的候选文本永不入库（保持既有纪律）。

## 迁移（v0 → v1）

- 触发：`Database` 初始化时无 `schema_meta` 且存在旧表，视为 v0，
  在接入开启前完成迁移（方案 §9：迁移只在启动期执行）。
- 方式：additive——只补 `messages` 缺列，新建 `schema_meta` /
  `delivery_turns` / `claims`；按 (user_id, turn_id) 从旧
  delivery 列回填台账（in_flight 一律转 ambiguous，与启动规则
  一致），随后删除不可见回合（prepared/ambiguous/failed）的
  assistant 旧行，user 行与可见行不动。旧 delivery 列自此冻结
  不再写入。
- 守恒验证（`tests/test_memory_p5.py`）：手工 v0 库迁移后，新
  get_history 输出与旧过滤口径逐字一致；各回合台账 state /
  accepted_text 与旧值对应（in_flight→ambiguous 除外）。

## 回滚演练记录（P5，测试库）

2026-10-31 于 `tests/test_memory_p5.py` 的回滚演练用例执行并通过：

1. 建 v0 库并写入基线数据；
2. 按 §9 规程做一致性快照：WAL checkpoint 后用 SQLite backup
   API 复制（未直接拷贝运行中的库文件）；
3. 用新代码打开（自动迁移到 v1）、收一条新消息、执行一次忘掉；
4. 回滚：以快照覆盖库文件，用旧 schema 口径（v0 表结构直接 SQL）
   打开——迁移前数据完整可读，服务可按旧代码继续运行；
5. **已知数据损失面（与 §9 口径一致）**：第 3 步中新收的消息与
   忘掉的删除都随快照恢复而回滚（新消息记录丢失、已删数据
   复现）。不存在零损失回滚；现网切换记录必须写明该损失面。

## 投递契约（§5.2）

- `DeliveryService`（`companion/services/delivery.py`）是额度与
  并发的唯一记账方：回复路径、危机直发、关怀发送经它预留/发送；
  危机告警经它的 `send_alert` 发送、由网关在 alert 策略内以同一
  SendQuota 记账；适配器只是传输，携带 `DeliveryHandle` 中的
  预留 token（OneBotAdapter 已透传），不自行计费。
- 告警回执口径在 P5 有一处刻意收紧：发送端返回 None 不再视为
  「已通知」（旧内联判定曾把 None 计为已发送）——按 §5.7，
  「已通知真人」模板变体只能建立在回执确认之上，None 不是确认。
  生产发送端恒返回 dict，该收紧只影响异常形态的注入端。
- `CareSendGate` 不再硬依赖 `HttpOneBotClient`：构造接受
  DeliveryService（或任意 sender，内部包装为 DeliveryService），
  关怀发送经 DeliveryService 走同一额度账本。
- 回执三态口径与适配器统一：accepted / rejected / ambiguous；
  ambiguous 永不自动重发。
