# NapCat 事件覆盖矩阵（N0 交付物）

来源：全接口接入方案 §2.3 × 调研报告 §4 事件表 × 本机 bundle 事件词表证据。
机器可读版：`tests/fixtures/napcat/event_coverage.json`（与本表同源生成）；一致性由
`tests/test_napcat_registry_n0.py` 断言。处置口径：入链 = 进场景子链处理；
登记 = 结构化落日志/指标、不触发业务（事件类不建 claim，方案 §4 C6）。

| post_type | type | sub_type | 方向 | 来源 | 处置 | 验证状态 | 备注 |
|---|---|---|---|---|---|---|---|
| message | private | friend | inbound | 报告+bundle+方案 | 入链（私聊链） | 双源/代码证实 | 现行行为不变 |
| message | private | group_temp | inbound | 报告+bundle+方案 | 入链（私聊链） | 双源/代码证实 | 报告记 sub_type=group + temp_source；bundle 代码证实 group_temp；单列 chat_type 不并入普通私聊 |
| message | group | normal | inbound | 报告+bundle+方案 | 入链（群聊子链） | 双源/代码证实 | 群聊子链；默认产品行为=不回复（能力接入、开关控制） |
| message | group | anonymous | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 匿名无稳定 user_id，不进业务链 |
| message | group | notice | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 系统提示型消息 |
| message_sent |  |  | self_echo | 报告+bundle+方案 | 登记 | 双源/代码证实 | 方向契约：只入观测与投递关联路径，永不进对话 claim 键空间、永不触发 Brain；需开 reportSelfMessage，默认不开 |
| notice | group_upload |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 群文件动态 |
| notice | group_admin | set/unset | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 |  |
| notice | group_decrease | leave/kick/kick_me | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | kick_me（机器人被踢）升为运维告警（observer） |
| notice | group_increase | approve/invite | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 |  |
| notice | group_ban | ban/lift_ban | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 机器人被禁言升为运维告警 |
| notice | friend_add |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 |  |
| notice | group_recall |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 只记元数据，不尝试取回内容 |
| notice | friend_recall |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 同 group_recall |
| notice | group_card |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | NapCat 特有 |
| notice | group_msg_emoji_like |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | L1 互动的数据面 |
| notice | essence | add/delete | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 |  |
| notice | online_file_receive |  | inbound | bundle+方案 | 登记 | bundle 独有补登记 | bundle 独有补登记：notice_type 强证据（见 bundle 快照 event_string_evidence，含 sub_type=cancel）；与在线文件族对应 |
| notice | online_file_send |  | inbound | bundle+方案 | 登记 | bundle 独有补登记 | bundle 独有补登记：notice_type 强证据；与在线文件族对应 |
| notice | notify | poke | inbound | 报告+bundle+方案 | 入链（事件子链） | 双源/代码证实 | 可触发轻互动响应（产品开关默认关） |
| notice | notify | lucky_king | inbound | 报告 | 登记 | 单源存疑 | 单源存疑：仅调研报告/官方事件表列出；bundle 全文出现 0 次，无事件证据。红包运气王只作事件存在——无发红包接口 |
| notice | notify | honor | inbound | 报告 | 登记 | 单源存疑 | 单源存疑：bundle 无 notice/sub_type 事件证据（子串仅 honorlist URL 与 get_group_honor_info 动作名） |
| notice | notify | title | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 头衔变更 |
| notice | notify | profile_like | inbound | 报告+bundle+方案 | 登记 | bundle 证实（N0 升级） | N0 升级：原第三方 SDK 索引，bundle 有 sub_type 事件证据（快照 event_string_evidence） |
| notice | notify | input_status | inbound | 报告+bundle+方案 | 登记 | bundle 证实（N0 升级） | N0 升级：原第三方 SDK 索引，bundle 有 sub_type 事件证据 |
| notice | notify | gray_tip | inbound | bundle+方案 | 登记 | bundle 独有补登记 | bundle 独有补登记：sub_type 强证据 |
| notice | notify | group_name | inbound | bundle+方案 | 登记 | bundle 独有补登记 | bundle 独有补登记：sub_type 事件证据 1 处（其余 22 处子串为字段/模式/赋值） |
| notice | bot_offline |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 登记 + 运维告警，与连接健康模块对账 |
| request | friend |  | inbound | 报告+bundle+方案 | 入链（事件子链） | 双源/代码证实 | 与 set_friend_add_request 配套；处置默认人工/策略关 |
| request | group | add/invite | inbound | 报告+bundle+方案 | 入链（事件子链） | 双源/代码证实 | 与 set_group_add_request 配套 |
| meta_event | lifecycle |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 |  |
| meta_event | heartbeat |  | inbound | 报告+bundle+方案 | 登记 | 双源/代码证实 | 与健康探针口径对账：现行以 get_status 轮询为准，heartbeat 只作冗余信号 |

## 自审记录（N0）

- **单源存疑 2 项**：notify 的 `lucky_king`、`honor` 仅有调研报告/官方事件表来源，bundle 全文核查无事件证据（lucky_king 出现 0 次；honor 子串仅 honorlist URL 与动作名），按方案 §2.6 结论 6 降为单源存疑，N3 落地时以此口径登记、不作已证实事件处理。
- **bundle 独有补登记 4 项**：notice `online_file_receive`、`online_file_send` 与 notify `gray_tip`、`group_name`，均有 bundle 强证据（notice_type/sub_type 赋值上下文），与在线文件动作族对应。
- **N0 升级 2 项**：notify `profile_like`、`input_status` 原为第三方 SDK 索引来源，bundle 有 sub_type 事件证据，验证状态升为 bundle 证实。
- **方向契约**：`message_sent`（自身消息回流）单列方向 self_echo，只入观测与投递关联路径，永不进对话 claim 键空间、永不触发 Brain（方案对撞补强口径）。
- post_type / message_type / meta_event_type / request_type 四个维度与调研报告零差异（方案 §2.6 结论 6）。
- 群临时会话：报告记 sub_type=group + temp_source，bundle 代码证实 group_temp，本表以 group_temp 登记并注明双记法。

## N3 落地注记

N3（2026-10-31）入站事件面已按本表处置逐行落地，机器可读版
`tests/fixtures/napcat/event_coverage.json` 每行已追加
`implemented: true` 与 `behavior_verified: true`，顶层另有
`n3_implementation` 说明键；本节只作落地注记，不改上表任何一行。

- **入链（私聊链）**：`message/private/friend` 与 `message/private/group_temp`
  已实现并保持现行行为不变——私聊 11 Stage 链顺序契约逐字不变，
  group_temp 单列 `chat_type`、仍入私聊链。私聊行为与日志零变化。
- **入链（群聊子链）**：`message/group/normal` 已实现为群聊子链的观察
  登记（`event_type="group_message"`）：只做解析 → 分类 → 有界观察
  （危机关键词命中只登记 `reason_code="crisis_keyword"`，不进私聊危机
  链、不发固定话术、不触发真人告警；生成/记忆读写/工具/自动动作全关，
  默认不回复；N4 起产品开关 `tools.group_chat.reply_enabled` 开
  且接线层注入 responder 时，观察之后可走 @ 触发的回复通路，
  生产默认关、无 responder 注入）。匿名（anonymous）与系统提示型（notice）群消息同走
  观察登记，`reason_code="registered_only"`；群队列满时 shed 并记
  `reason_code="shed"`，群流量永不挤占私聊/危机通道。
- **登记（notice）**：全部 notice 子类已实现结构化登记
  （`event_type="notice"`），状态类 notice（group_card、input_status
  等）同键合并只留一条、合并计入 `companion_events_total`
  的 coalesced 口径；运维升级三类在 notice 记录之后追加
  `event_type="ops_alert"`——bot 被踢（group_decrease/kick_me，
  `reason_code="bot_kicked"`）、bot 被禁言（group_ban 且
  user_id == self_id，`reason_code="bot_muted"`）、bot_offline
  （`reason_code="bot_offline"`）。
- **入链（事件子链）+ 处置状态机（request）**：friend 与 group
  （add/invite）request 已实现入事件子链处置：落库处置状态机
  （`request_dispositions` 表，唯一键
  `(self_id, request_type, sub_type, flag)`，pending → processing →
  done/ambiguous/failed），默认决策为人工（done/manual、不自动通过）；
  经 Fake 通道（GatewayRequestChannel + FakeAdapter 网关）端到端验证
  「只处置一次」——重复投递只产生 duplicate 登记、处置通道不二次
  调用，通道结果 ambiguous 时落 ambiguous 终态、不释放不重驱。
  notify/poke 同入事件子链面，轻互动响应产品开关默认关、只登记。
- **登记（meta / message_sent）**：lifecycle/heartbeat 已实现登记
  （`event_type="meta_event"`，heartbeat 记录带 online/good 状态
  字段）；`message_sent` 自身回流的方向契约已落地——只登记
  （`event_type="message_sent"`）、永不进对话 claim 键空间、永不
  触发 Brain、零回复。
- **仍单源存疑 2 项**：notify 的 `lucky_king`、`honor` 的**处理已
  落地**（同其余 notice 一样登记处置，golden 用例已覆盖登记行为），
  但**平台存在性未证实**——仅官方事件表来源、本机 bundle 无事件
  证据，verification 保持 `single_source_doubtful` 不改判；golden
  报文按官方事件表构造，只断言本框架的登记处置、不断言平台必发
  此事件。
- **heartbeat 对账规则（C16）已落地**：连接健康以 `get_status`
  轮询探针为准，heartbeat 事件只作冗余信号登记，两者不一致时以
  探针结论为准、不以 heartbeat 覆盖探针状态；bot_offline 事件与
  探针失联互相印证时按 ops_alert 处置，单有 heartbeat 异常不单独
  升级告警。
