# NapCat 待验两条复核记录（2026-11-01）

阶段：N6 收口后专项复核。基线：现网 tip `f8cea62`；作业分支
`ai/qqbot-action-verify`（独立 worktree，未合并、未部署、生产零改动）。
对象：登记表仅剩的两条 `pending_verification`——`get_group_todo`、
`delete_unidirectional_friend`。

**验证纪律**：`get_group_todo` 为只读查询语义，静态核实后经现网
NapCat HTTP API 做真实只读调用复证；`delete_unidirectional_friend`
为删除语义，**只许静态验证、全程未实调**。

**证据基座**：本机 NapCat **4.18.41** bundle `napcat.mjs`
（3,134,858 字节，MD5 `7fdb4903c8867e7db0b3345ad226c805`，与 N0
快照记录逐字一致，即同一份字节）；运行实例 `get_version_info`
复核为 app_version 4.18.41 / OneBot v11，与登记表 `NAPCAT_VERSION`
一致，排除「运行构建与磁盘 bundle 漂移」的可能。

**方法（较 N0/N6 的升级点）**：N0 用注册表对象抽取、N6 用动作类
抽取；本轮对 bundle **全文**做穷举字符串搜索，覆盖注册表枚举、
动作类、核心 API 方法层、WebUI Debug 示例表及其余全部代码，并对
`get_group_todo` 做第二次现网实调（带同场负对照）。

## 1. get_group_todo → 改判 `nonexistent`

静态证据：

- 字符串 `get_group_todo` 在 bundle 全文 **0 出现**；
  `GetGroupTodo` 标识符同样 0 出现。
- 注册表枚举 `const re` 中待办族仅 `SetGroupTodo` /
  `CompleteGroupTodo` / `CancelGroupTodo` 三名，无查询名。
- 核心 API 层仅有 `async SetGroupTodo` / `CompleteGroupTodo` /
  `CancelGroupTodo` 三个 Oidb 构建发送方法，无任何待办查询方法。
- WebUI Debug 示例表仅有 Set/Complete 两条 payload 示例
  （`group_id` + `message_id`），无查询条目。
- 与 N6 动作类抽取 found=false（`docs/napcat-schema-evidence-n6.md`
  §2）互相印证；本轮把「不在注册表」加强为「不在 bundle 任何一层」。

实调证据（2026-11-01，现网实例，HTTP POST 127.0.0.1:3000）：

- 正对照 `get_group_list` 正常返回机器人群列表，API 通道健康；
  目标取机器人专用测试群（群号不入仓）。
- `get_group_todo` 带真实 `group_id` 调用 →
  `{"status":"failed","retcode":200,"data":null,"message":"不支持的Api get_group_todo"}`；
  空参调用同结果。
- 同场负对照 `get_nonexistent_probe_20261101`（本轮构造的不存在
  名）→ 同结构、同文案，仅动作名不同。
- 若动作存在，分发层不会以「不支持的Api」拒绝带合法参数的
  请求；此为分发层未注册的特征响应。与 N0（2026-10-31）实调
  结论一致，两次独立实调 + 全文穷举，证据闭合。

裁决：群待办**查询动作在 NapCat 不存在**，与 `send_red_packet`
同格，`disposition` 改判 `nonexistent`；`existence=absent`、
`schema=unverified` 不变（无 schema 可验）。产品不得规划群待办
查询功能。恢复路径不变：NapCat 升级时按 `review_point` 重跑
三方对账，出现新事实按新证据再改判。

## 2. delete_unidirectional_friend → 改判 `not_supported`

静态证据（仅静态，未实调）：

- 全文穷举：`delete_unidirectional_friend` 仅 **1 处**出现，即
  注册表枚举中的注释行
  `// DeleteUnidirectionalFriendList : 'delete_unidirectional_friend',`
  （快照 fixture `bundle_registry_snapshot.json` 的 commented 集
  在册）；`DeleteUnidirectionalFriend` 标识符亦仅此 1 处。
- 无动作类、无核心方法。对照其读侧
  `get_unidirectional_friend_list` 在同一 bundle 中有完整动作类
  （`actionName` 绑定、payloadSchema 空对象、returnSchema 为
  单向好友数组）——同族一读一删只实现读，删除名系**有意注释掉**。
- 官方文档标 ❌（方案 §2.5）；调研报告「不支持」8 名中此名在列
  且仅注释行（`docs/napcat-verification-n0.md` §1）。

冲突裁决：唯一反证为 Apifox 索引有列。按方案 §2.6 结论 3 的
冲突解决原则「以本机为准」，且与本条证据形态完全相同的四条
（`set_group_anonymous`、`set_group_anonymous_ban`、
`qidian_get_account_info`、`reload_event_filter`：官方不支持 +
bundle 仅注释行）均已登记 `not_supported`，同形同判。三方冲突
就此销账，`disposition` 改判 `not_supported`；
`existence=absent`、`schema=unverified` 不变。恢复路径不变：
升级重对账若注释行转为未注释在册，按 L2 关系处置口径补接。

## 3. 计数与影响

| 口径 | 复核前 | 复核后 |
|---|---|---|
| 登记表总条目 | 191 | 191 |
| pending_verification | 2 | **0（清零）** |
| not_supported | 4 | 5 |
| nonexistent | 1 | 2 |
| `integrated_count()` | 167 | 167（两条本就未接入） |
| schema verified | 138 | 138（两条无 schema 可验，维持 unverified） |

- 运行时影响为零：两条均非 `registered`，Gateway 不派生
  ActionSpec；`tests/test_gateway_n1.py` 的不可派发断言不受影响。
- 同步改动：`companion/adapters/action_registry.py` 两条条目与
  模块 docstring 追记、`tests/test_napcat_registry_n0.py` 处置
  计数与定名断言、`docs/napcat-integration-closeout.md` 挂起
  清单加复核销账注记。
