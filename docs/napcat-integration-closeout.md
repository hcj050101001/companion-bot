# NapCat 全接口接入收口（N 系列 N0–N6）

本文是「NapCat 全接口接入」路线图（方案
`plans/2026-10-31-napcat-full-api-integration.md`）的全线收口记录：
N0 验证登记 → N1 传输与网关收敛 → N2 消息形态 → N3 入站事件面 →
N4 群场景与 L2 管理 → N5 文件媒体域 → N6 敏感面与收口，七个阶段
全部完成。口径基准为本机 NapCat **4.18.41** bundle 注册表对象
（未注释条目为准，与官方文档冲突时以本机为准）。

**最终数字**：登记表全量 **191** 条，全部有终局处置、无无主条目；
其中 `registered` **172** 条（canonical 167 + 别名 5），
**已接入 167 条 = 全部 canonical registered 条目**；schema 经
bundle 证据定稿（verified）**138** 条。计数由
`companion/adapters/action_registry.py` 的 `integrated_count()` 与
`tests/test_napcat_registry_n0.py` 的契约测试钉死，不是文档口径。

## 1. 已接入面（167 条）

分域（canonical、implemented + behavior_verified 双真）：

| 域 | 条数 | 阶段分布 |
|---|---|---|
| A 消息 | 28 | L0 查询 N1；发送/转发形态 N2；L2 delete_msg/快捷操作 N4 |
| B 群管理 | 41 | L0 N1；L1 签到/待办写 N4；L2 管理族 N4 |
| C 好友/社交 | 22 | L0 N1；L1 互动 N2；L2 关系处置 N4 |
| D 媒体处理 | 10 | L0 N1（含 OCR 经网关资源边界 N5） |
| E 文件 | 14 | 读 N1/N5；写 9 条 N5 |
| E2 闪传/在线/流式 | 18 | 读 N5；写 12 条 N5（段形态见 §4 残留） |
| F 群相册 | 9 | 读 N5；写 7 条 N5（熔断保护，见 §3） |
| G 资料/空间 | 11 | L0 N1；L1 N2；L2 资料族 N4；qzone N4 迁 send_qzone_msg |
| H 系统/敏感 | 14 | L0 系统查询 N1；L2 clean_cache N4；**L3 八条 N6**（默认硬禁 + 受控解锁，见 §2） |

分级：L0 61 / L1 62 / L2 36 / L3 8。

别名 5 条（网关在派发前归一到 canonical，不单独计数）：
`get_group_ignore_add_request → get_group_ignored_notifies`、
`set_group_sign → send_group_sign`、`.ocr_image → ocr_image`、
`ArkSharePeer → send_ark_share`、`ArkShareGroup → send_group_ark_share`。

统一调用面：一切出站动作经 `ActionGateway` 单一收口（spec 查表 →
主体 → 分级门禁 → 参数/资源校验 → 幂等账本 → 策略/限流/确认 →
额度 → 派发 → 白名单化审计行），9 个类型化门面
（Messaging/Group/Social/Media/File/Album/Profile/System/Sensitive）
是唯一调用形态；入站经四型判别联合分类（消息/通知/请求/元事件），
群链只观察、不进私聊危机链。

## 2. L3 受控解锁（N6 落地）

L3 八条（get_cookies / get_csrf_token / get_credentials /
get_clientkey / nc_get_rkey / get_rkey / get_rkey_server /
send_packet）的默认状态与 N1 起一致：**对一切主体硬禁，
policies 写 enabled=True 也无效，传输层零调用**。N6 只补一条
受控解锁路径，语义如下（实现：`adapters/gateway.py` 构造参数与
invoke 步骤 c/c3；证据：`tests/test_l3_unlock_n6.py`）：

- **解锁面**：运维配置段 `actions.l3_unlock`（逐动作显式名单，
  Pydantic 校验拒绝通配/重复/非 L3 名）→ 进程启动时经 ingress →
  HttpOneBotClient 注入网关构造参数，构造期快照为 frozenset；
  `actions.*` 不在热更新白名单内，**改名单必须重启进程**；
  `invoke()` 无任何解锁参数，运行时与入站消息均无解锁路径；
  构造后变更源集合/配置对象不影响已运行实例（测试钉死）。
- **解锁后门禁不减**：主体必须是 ops_cli（其余四主体含 llm_tool
  一律 principal_forbidden）；必须带 AuthorizationContext 且
  account 范围覆盖（L3 目标恒派生为 account，注入 group/user
  参数不能降格）；参数规则照常（domain、cmd+data 必填）；
  策略层可单独再关（enabled=False）；dry_run/限流/账本照常。
- **审计**：放行与拒绝都落同一行审计（主体/授权人/动作/
  policy_key/结果/gate/耗时）；**返回值永不入日志**——凭证类
  返回只以「结果码」形态出现在审计行，data 只回调用方
  （caplog 全量断言钉死）。
- **收敛性**：运行时代码中 L3 门面零调用点（AST 扫描测试钉死，
  门面定义文件除外）；生产配置 `actions.l3_unlock` 为空。

## 3. 启用开关一览（位置与默认状态）

| 能力 | 开关 | 默认 | 备注 |
|---|---|---|---|
| 群聊回复 | `tools.group_chat.enabled` + `tools.group_chat.reply_enabled`（config） | 双关 | 两者都开才装配回复通路；仅 @ 机器人触发；responder 无私聊记忆/工具。**是否开启是产品决策，未拍板** |
| 群发送（段通道） | 网关策略（动作 policy）+ `DeliveryService(group_delivery_enabled)` | 双关 | 两级额度（群桶 + 账号群子预算）已就位；无生产调用方 |
| request 自动处置 | `RequestPolicy` 模式（代码接线，ingress 装配） | `manual` | allowlist 模式只自动同意名单内、永不自动拒绝；生产处置 channel=None（只落库待人工） |
| L3 解锁 | `actions.l3_unlock`（config，逐动作名单） | 空（全硬禁） | 改名单须重启；见 §2 |
| L2 动作 | 网关策略 + canonical 路径默认 `dry_run` | dry_run=true | 真实执行须接线层显式关闭 dry_run 并持授权上下文；不可逆档另须一次性确认令牌 |
| 群相册熔断 | `ActionCircuitBreaker`（网关内建，F 域） | 开（阈值 3 次连续派发失败 / 冷却 300 s） | 半开单探针；N5 Review 修复后探针取消/超时不死锁 |
| 主动关怀 | `proactive_care.enabled`（config） | 关 | 框架线 D2 决策：opt-in、默认关，与现网一致 |
| 私聊发送 | 网关策略 `send_private_msg` | 开 | 现网既有行为，框架线保持字节等价（golden 钉死） |

## 4. 刻意不接 / 挂起清单（终局处置，24 条非 canonical 条目）

`excluded_internal`（6，平台内部/测试形态，非接口）：

| 条目 | 理由 |
|---|---|
| clean_stream_temp_file | 流传输内部清理（N0 定性） |
| test_auto_register_01 / test_auto_register_02 | 平台测试名（N0 定性） |
| unknown | 注册表占位键（N0 定性） |
| test_download_stream | **N6 改判**：N5 handler 定性为 NapCat 侧测试脚手架——合成流、不调核心 API、只产合成文本块；与 test_auto_register 同族 |
| .get_word_slices | **N6 改判**：注册表有键但全 bundle 无 handler 类（键名仅出现 1 次，证据见 `docs/napcat-schema-evidence-n6.md` §2）；门面已下线。恢复路径：升级重抽取出现动作类或值守 Debug 实调证实 |

`ops_only`（2，只走 runbook、永不进运行时与解锁面）：

| 条目 | 理由 |
|---|---|
| bot_exit | 登出为运维灾难级动作；N6 已补抽 schema 证据（payload/return 均空），处置终审维持 ops_only |
| set_restart | 官方标不支持 × bundle 在册的冲突以本机为准确认存在，但日常重启归 systemd/看门狗，运行时不需要；同 bot_exit 维持 ops_only |

`deprecated`（2）：`get_guild_list`、`get_guild_service_profile`
——频道（guild）体系已废弃，只登记不接入。

`not_supported`（4）：`set_group_anonymous`、
`set_group_anonymous_ban`、`qidian_get_account_info`、
`reload_event_filter` ——官方明示不支持且 bundle 仅注释行/无登记，
方案明示真不接。

`nonexistent`（1）：`send_red_packet` ——全部官方清单无发红包 API
（仅有 lucky_king 事件），产品不得规划红包功能。

`not_registered`（2）：`reboot_normal`、`upload_forward_msg`
——仅见于 bundle 注释行，从未注册。

`pending_verification`（2，挂起待验，写明路径、不静默关闭）：

| 条目 | 挂起原因 | 未来验证路径 |
|---|---|---|
| get_group_todo | bundle 注册表无此名（N6 抽取复证 found=false）；N0 实调回「不支持的Api」与负对照同形；但待办写三名在册，查询名不排除未来补齐/别名 | NapCat 升级时重跑三方对账与动作类抽取；或值守 WebUI Debug 全表浏览证实 |
| delete_unidirectional_friend | 三方冲突未解：官方标 ❌、Apifox 有列、bundle 仅注释行 | 同上：升级重对账；若升级后注释行转未注释在册，按 L2 关系处置口径补接 |

> **2026-11-01 复核销账（本节表格保留 N6 收口时点原貌）**：上表
> 两条已在分支 `ai/qqbot-action-verify` 完成复核并改判——
> `get_group_todo` 经 bundle 全文穷举（0 出现）+ 第二次现网实调
> （与同场负对照同形）改判 `nonexistent`；
> `delete_unidirectional_friend` 经全文穷举（仅注释行 1 处）+
> 「以本机为准」冲突裁决改判 `not_supported`（全程仅静态验证，
> 未实调）。处置计数随之更新：pending_verification 2 → 0、
> not_supported 4 → 5、nonexistent 1 → 2，总条目 191 与已接入
> 167、schema verified 138 均不变。证据全文见
> `docs/napcat-action-verify-2026-11-01.md`；两条的升级重对账
> 恢复路径不变。

WebUI-only 5 类（Debug/文件管理/凭据/热重载/信息查询）不算动作
条目：只作运维通道，处置见 `docs/napcat-ops.md` 的专节，永不经
Gateway 运行时接入。

## 5. 口径裁决留痕（N6 终审）

- **28+1 个 p.Any return**：维持三态 + 平台 data 透传，不编造
  字段级 return 契约（理由与立项条件见
  `docs/napcat-schema-evidence-n6.md` §3）；残留登记 R-N1。
- **folder_id 差异**：`get_group_files_by_folder` 的 bundle schema
  为 Optional、网关保持必填（更严）——维持，记为已知差异
  （R-N2）：folder 语义歧义时 fail-closed 优于误查根目录。
- **file:// 百分号解码错位面**：ResourceResolver 对 file:// URI 的
  解码与平台口径存在错位可能，接受为残留（R-N3）：资源面只经
  网关校验后透传，错位后果是平台拒绝（fail-closed 方向）。
- **signed_url 敏感启发式漏标面**：签名 URL 的 sensitivity 标记
  靠启发式，漏标可能使可签名 URL 以普通资源形态流转，接受为
  残留（R-N4）：审计行只记摘要不记原文，漏标不导致原文入日志。
- **ResourceResolver 会话绑定运行时接线**：定位确认——绑定/过期
  机制已完整实现且网关注入点就位，但生产 ingress 未注入
  resolver（生产装配走 resource_roots 口径）；**运行时接线归
  切现网装配决策**，不是契约缺口（R-N5）。
- **§3.4 action_levels**：已落地——`AdapterCapabilities.action_levels`
  由 `build_action_levels()` 从 ACTION_SPECS 派生，Fake 与
  OneBot 两适配器同源同值，契约测试钉死。
- **终审 Review 修复（Claude Code，P2-1）**：终审发现 E2 读族
  五名（get_flash_file_list / get_flash_file_url /
  get_fileset_info / get_fileset_id / get_share_link）的门面
  参数名与 bundle schema 实证不符（旧签名实调必 400，属
  N1/N5 落地时 schema 待验的臆测签名残留），已按证据改签名、
  补网关必填规则与夹具对齐契约测试——本收口的 167 计数自此
  与可调用契约一致。细节见 CHANGELOG 的 N6 Review 修复条目。

## 6. 不变量与证据索引

方案 §6 N6 验收的七项不变量，各自的证明位置：

1. 私聊行为不变（golden + 对话剧本）：`tests/test_golden*`、
   私聊全链路既有测试（全套回归每阶段绿）。
2. 群消息永不进私聊危机链：N3 群子链测试（含群「想自杀」零发送
   与私聊同文危机对照）。
3. 群链默认只观察：群回复双开关默认关（§3），N4 测试钉死。
4. 账号级预算上限：`companion/quota.py` 两级额度 + N2 额度测试
   （critical 池不可借、不可自称）。
5. L3 默认硬禁：N1 起每阶段回归；N6 解锁不改变默认
   （`tests/test_l3_unlock_n6.py`）。
6. 凭证返回值不入日志/记忆：网关审计行白名单化 + N6 caplog
   全量断言；文件域只记摘要（N5）。
7. 切现网可回滚：框架/N 系列全程独立分支、现网未动（§7）。

## 7. 与现网的关系（切现网是单独决策）

本路线图全程**未部署、未重启现网服务、未改生产配置**：现网仍是
多气泡线版本独立运行。框架线（含 N 系列）切现网按
`docs/deployment-runbook.md`（P7 runbook）§5 的步骤与回滚口径
执行，**是否切、何时切由用户单独决策**，不在本收口范围内。
切现网前置项：真机冒烟留痕（普通私聊/危机/工具/多气泡）、
`v0.9.0` tag 位置决定、resolver 运行时接线口径（§5 R-N5）与
群回复产品开关（§3）的一并拍板。
