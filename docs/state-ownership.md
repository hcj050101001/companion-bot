# 状态归属登记表（方案 §5.6）

全部运行时状态逐项登记归属与重启语义。**禁止新增无登记的全局状态**：
新状态落地前先在本表登记（存储、归属模块、重启语义），再写代码。

| 状态 | 存储 | 归属 | 重启语义 |
|---|---|---|---|
| 入站去重 claim（键 platform/self_id/message_id、分区、代次、终态） | 落库：memory.db `claims` 表 + `schema_meta`（claim_generation / claim_exempted） | `memory.claims.SqliteClaimStore`（P5 落库，D3） | 保留；新代次启动时上代 processing 一律转 abandoned 不重放；终态 24h 后可淘汰 |
| 短期对话历史（40 行窗口）与历史投影（含 role=vision 识图观察行：已投递图片回合的描述经结构化隔离后以外部素材框架落存、无 TTL、计入同一窗口，插入即同口径裁剪） | 落库：memory.db `messages` | `memory.conversation.ConversationStore` | 保留；assistant 行只存已接受前缀的投影；观察行不参与投递状态机，启动恢复的 in_flight→ambiguous 只删 assistant 投影、不动观察行；「忘掉」连观察行一并删，危机认领按登记集 + 图像同一性补偿删除（同一性不可判时全删该用户 vision 行） |
| 识图观察同一性索引（turn_id → 图片内容哈希） | 落库：memory.db `vision_observation_index` | `memory.conversation.ConversationStore` | 保留；随观察行落存/删除/窗口裁剪/「忘掉」在同事务内同步维护，不独立存在；供危机认领跨上传定位同图观察行，重启后仍可用 |
| 投递台账（prepared/in_flight/partial/complete/ambiguous/failed + unsent 计数 + 已接受文本） | 落库：memory.db `delivery_turns`（与历史同库不同表） | `memory.ledger.DeliveryLedger` | 保留；启动时 in_flight 一律转 ambiguous（不重放、其前缀不进历史）；终态回合的历史行被裁剪后，其台账行在下次打开时修剪（不无界增长） |
| 投递代次（delivery_generation） | 落库：memory.db `delivery_generations` | `memory.ledger` | 保留；新事件/忘掉时 +1，围栏迟到的投递写 |
| 人设包选择与代次（pack_preferences） | 落库：memory.db | `memory.conversation` | 保留；忘掉时清空选择并 +1 代次 |
| onboarding 已告知标记（user_state） | 落库：memory.db | `memory.conversation` | 保留 |
| 关怀选择与发送回执（choices / receipts） | 落库：仓外 0600 SQLite（路径纪律不变） | `memory.care.CarePreferences` | 保留；同意 ≤30 天过期、回执保留 2 天，到期修剪 |
| 长期记忆 | 未落库：端口已定、实现 deferred | `memory.LongTermMemory`（stub） | 无状态；实现留待后续阶段（Chroma 另立课题） |
| 发送额度与并发槽（每人 18/60s、全账号 60 + critical 10/60s、并发 2/普通 1） | **进程内** | `quota.SendQuota`（DeliveryService 独占记账） | **重启重置——显式接受**：额度是防滥用节流而非账目，重置只会短暂放宽，不产生重复发送（发送本身由台账/claim 守卫） |
| 入站限流（6/min/user 滑动窗口） | **进程内** | `ingress.RateLimiter` | **重启重置——显式接受**，同上 |
| 工具网关限流（ToolRateLimiter） | **进程内** | `tools.ToolGateway` | **重启重置——显式接受**，同上 |
| 危机告警去重（crisis_history，1h 窗口同级不重复；time 为告警锚点、只在 first/upgrade/窗口过期时重置） | **进程内** | `ingress.EventProcessor` | **重启重置——显式接受**：最坏后果是重启后同一危机多发一次告警；危机话术本身的去重由落库 claim 保证，不依赖本状态 |
| 关怀阻塞时钟（_last_crisis_at，每用户最近一次危机事件时刻；P6 新增） | **进程内** | `ingress.EventProcessor` | **重启重置——显式接受**：与告警锚点分开记账（每次危机事件都推进，含告警 await 窗口内），只供 CareSendGate 的 current_care_safety 实时重查判阻塞；重置最坏后果是重启后 crisis_window 内的关怀阻塞提前解除，关怀发送本身仍有同意/静默/安全 detect 多重门 |
| 关怀待发队列与撤回纪元（CareScheduler._pending / _epochs；P6 新增） | **进程内** | `scheduler.CareScheduler` | **重启丢失——显式接受**：队列本就无生产入队方（触发参数属 D2 另立阶段）；丢失只少发、不重发——同意与发送回执都在仓外落库（见关怀选择行），日上限与防重不依赖本队列；epoch 随队列一并丢失，无跨重启戳记可混淆 |
| 调度任务表（InProcessScheduler._entries：runner/interval/runs/last_error；P6 新增） | **进程内** | `scheduler.InProcessScheduler` | 进程内并发原语，无持久语义；任务在 create_app 组合根静态注册，重启后按配置重建 |
| onboarding 在途 claim（_onboarding_claims） | **进程内** | `ingress.EventProcessor` | 重启丢失——可接受：在途标记丢失只可能重复进入 onboarding 判定，已落库的 user_state 标记防重复发送告知 |
| 重复追问会话（repetition，会话 TTL 20min） | **进程内** | `repetition.RepetitionSessions` | 重启丢失——显式接受（方案 §5.6 登记口径） |
| 天气缓存（<10min、上限 128 条） | **进程内** | `tools.weather` 适配器 | 重启丢失——显式接受（方案 §5.6 登记口径） |
| 消费者二级去重缓存（seen_message_ids / pending_message_ids） | **进程内** | `ingress` 消费者收尾路径 | 重启丢失——可接受：权威去重是落库 claim，本缓存只是同进程内的快速路径（>10000 清空） |
| 每用户发送槽 / 在途流任务表（_send_slots / _tasks / _ordinary_streams） | **进程内** | `ingress.EventProcessor` | 进程内并发原语，无持久语义；关停 drain 10s 后取消，在途发送按台账 ambiguous 记账 |
| 连接健康采样状态（失败计数/退避） | **进程内** | `ops.connection_health` | 重启重置——只读探针，无副作用 |
| judge 信号量与等待队列（并发 2、等待 ≤5s） | **进程内** | `ingress` SafetyJudge 路径 | 进程内并发原语，无持久语义 |
| 流水线三道队列与执行池（200/50/20、池 8+2） | **进程内** | `ingress` EventBus / LaneExecutor | 队列内容不落库：重启时已 ack 未处理事件由上游重投，落库 claim 防重复处理 |
| 观测聚合（Stage 耗时/工具步/token/危机计数；P8 新增） | **进程内** | `pipeline.observers.StageMetrics`（`EventProcessor.metrics`） | **重启重置——显式接受**：指标是进程内累计、非账目，重置只丢观测窗口；按用户聚合的键为进程盐 HMAC，盐随进程更换、键不可跨重启比对（方案 §5.10） |
| 视觉接力记录（_vision_handoff，每用户 ≤1 条、TTL 120s，含 base64 图片 data URL 与计数/fetch_codes 元数据、crisis_disposed 危机处置标记与 observation_turn_ids 已落存观察回合集（危机迟到认领的补偿删除寻址）） | **进程内** | `ingress.EventProcessor` | **重启即失——显式接受**：丢失只使在途图片无法被后续文本接管，用户可重发，无重复发送风险（接管受风格/安全代次与代次顺序守卫约束）；清理点为 TTL 过期（接管时先于 consumed 判定丢弃、登记时检查丢弃）、接管命中即终态（置 consumed 后立即 pop 字典条目，来源回合凭对象引用判读 consumed；via_handoff 接管路径不再登记新记录）、来源/接管回合 Deliver 的 shielded 收尾按台账读回清理（有气泡实际发出、ambiguous、气泡已接受但记账写被围栏拒（record_fence）、或读回失败按可能已送达保守处理时清除；failed/无行等零发出时保留等 TTL 内接力；投递被取消或抛非取消异常时同样先收尾再上抛）、「忘掉」分支与危机命中分支的无条件清除；单账号单进程口径，不跨进程共享 |
| 视觉投递中标记（_vision_delivering，每用户计数；确认复审 R1 新增） | **进程内** | `ingress.EventProcessor` | **重启丢失——显式接受**：仅为防接管竞态的并发原语（视觉回合在 _stage_deliver 投递未落定期间置位、收尾完成后在 finally 清位，置位期间 _take_vision_handoff 拒绝接管不消费），无持久语义；丢失最坏后果是来源投递未落定期间记录被接管，与接力记录行同口径（台账读回收尾仍按已送达事实清记录防双回复） |
| 视觉已服务代次水位（_vision_served_generation，每用户已服务图片的最大投递代次；确认复审第二轮 P4-5 新增） | **进程内** | `ingress.EventProcessor` | **重启丢失——显式接受**：只升不降的顺序守卫——_take_vision_handoff 接管成功时以被接管记录代次抬升，_settle_vision_delivery 中来源回合自有接力记录因有发出被清除时以该记录代次抬升；_register_vision_handoff 对代次 ≤ 水位的迟到登记一律拒绝（已服务过的图片不被更旧图片补回复）；丢失最坏后果是重启后迟到旧回合的图片可能重新登记接力，与接力记录行同口径（台账读回收尾仍按已送达事实清记录防双回复），无重复发送风险由代次围栏与台账守卫兜底 |

落库存储的 schema 版本统一记 memory.db `schema_meta.schema_version`
（当前 1）与关怀库 `schema_meta.schema_version`（当前 1），迁移机制
见 [memory-delivery.md](memory-delivery.md)。
