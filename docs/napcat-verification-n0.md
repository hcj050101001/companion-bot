# N0 验证与登记固化记录（2026-10-31）

阶段：NapCat 全接口接入路线图 **N0（验证与登记固化）**。基线：框架线
`ai/qqbot-framework-v1` tip `c42a1f4`（905 passed / 0 xfailed）；本阶段分支
`ai/qqbot-napcat-n0`。方案依据：`plans/2026-10-31-napcat-full-api-integration.md`
§2/§6（N0 验收）/§7.3（验证阶梯）。

**本阶段零运行时行为变更**：交付物全部为测试夹具、纯数据注册表骨架、
覆盖矩阵与本文档；注册表骨架模块 `companion/adapters/action_registry.py`
不被任何运行时模块 import。验证纪律：实调只做 §7.3 第 2 级的只读核对——
仅 `get_*` 前缀动作、空参数、零状态变更；L3 凭证类（get_rkey 系、
get_clientkey 等）与可能返回个人数据的动作一律不调；写类动作的 schema
只靠 bundle 证据与官方文档核对，核不了的标待验。

## 1. 第 1 级：bundle 注册表核对（离线只读）

- 对象：本机 NapCat **4.18.41**（QQ 3.2.34-53644）`napcat.mjs`，
  MD5 `7fdb4903c8867e7db0b3345ad226c805`，3,134,858 字节；动作注册表对象
  `const re = {...}`（字节偏移 1076910 起）。
- 方法固化为 `tests/scripts_n0/extract_bundle_registry.py`；快照固化为
  `tests/fixtures/napcat/bundle_registry_snapshot.json`。两路独立抽取
  （脚本抽取与人工核查）结果逐项一致：**未注释 182 条、注释 8 条**。
- 注释 8 条：set_group_anonymous、set_group_anonymous_ban、
  qidian_get_account_info、delete_unidirectional_friend、reload_event_filter
  （报告「不支持」5 名，仅注释行）+ get_unidirectional_friend_list
  （同键另有未注释条目，注释行为重复登记）+ reboot_normal、
  upload_forward_msg（注释行独有，不登记）。
- 三方对账（与方案 §2.6 锚点逐项吻合，测试断言见
  `tests/test_napcat_registry_n0.py`）：
  - 调研报告支持类 136 名 **全部**在未注释注册表中（缺失 0）；
  - 报告「不支持」8 名中 3 名在册（分类冲突，以本机为准登记）：
    set_restart、get_unidirectional_friend_list、.get_word_slices；
    其余 5 名仅注释行；
  - 注册表独有候选 43 = 补登记 39 + 内部/测试名排除 4
    （clean_stream_temp_file、test_auto_register_01、test_auto_register_02、
    unknown）；
  - 算术：182 = 139（报告命名且在册）+ 43（注册表独有）；
    144（报告命名）= 139 + 5（仅注释行）。

## 2. 第 2 级：只读实调探针（HTTP POST 127.0.0.1:3000，参数 `{}`)

**与方案字面的偏离声明**：§7.3 第 2 级字面要求「值守在场用 WebUI Debug
只看 schema、不点调用」。本阶段执行时 WebUI 登录属值守动作、后台窗口不
擅自登录，故按本阶段执行纪律以 **HTTP `get_*` 只读探针**替代：仅
`get_*` 前缀动作、空参数、零状态变更。探针只能复证「在册 + 必填参数
存在」，**不能替代 schema 内容核对**——所有 schema 待验状态不因探针
升级（见 §4）；WebUI Debug schema 浏览仍为未欠事项，列入待验路径，
值守在场时执行。

回执两类泾渭分明：在册动作回 `failed / retcode 400 /
"Schema compilation error: Expected required property"`——动作已注册且
带必填参数 schema，空参在校验层被拦、**未执行、零状态变更、零数据返回**；
不存在的动作回 `failed / retcode 200 / "不支持的Api <name>"`。

| 动作 | 结果 | 结论 |
|---|---|---|
| get_ai_characters | 400 schema 校验错 | 在册复证；必填参数存在；schema 依官方文档（双源已证，探针不另证内容） |
| get_ai_record | 400 schema 校验错 | 在册复证；必填参数存在（校验层拦截、合成未触发）；schema 内容待验 |
| get_fileset_id | 400 schema 校验错 | 在册复证；schema 内容待验 |
| get_fileset_info | 400 schema 校验错 | 同上 |
| get_share_link | 400 schema 校验错 | 同上 |
| get_flash_file_list | 400 schema 校验错 | 同上 |
| get_flash_file_url | 400 schema 校验错 | 同上 |
| get_online_file_msg | 400 schema 校验错 | 同上 |
| get_group_signed_list | 400 schema 校验错 | 同上 |
| get_private_file_url | 400 schema 校验错 | 同上 |
| get_emoji_likes | 400 schema 校验错 | 同上 |
| get_group_detail_info | 400 schema 校验错 | 同上 |
| get_group_share_link | 400 schema 校验错 | 同上 |
| get_group_todo | 200 不支持的Api | 与负对照同形：当前版本不支持（见 §4） |
| get_n0_nonexistent_probe | 200 不支持的Api | 负对照基准（本阶段构造的不存在名） |

未实调（按纪律）：全部 L3 凭证类（返回值即密钥材料）、非 `get_*` 前缀的
.get_word_slices、一切写类动作、可能返回个人数据的查询
（get_friends_with_category、get_doubt_friends_add_request 等——存在性
已由 bundle 在册证实，无需以真实数据为代价复证）。

## 3. 定名结论（N0 落定）

| 项 | 结论 |
|---|---|
| 群文件移动/改名 | canonical = **trans_group_file**（注册名）；move_group_file、rename_group_file 在 bundle **并存注册**，登记为别名条目；三名主名映射以 N5 schema 核对为准 |
| 被忽略入群申请 | canonical = **get_group_ignored_notifies**（注册名）；get_group_ignore_add_request 并存注册，登记为别名条目 |
| 在线文件拒绝 | 确切名 **refuse_online_file**（非 reject） |
| 在线文件消息查询 | 确切名 **get_online_file_msg** |
| 删相册媒体 | 确切名 **del_group_album_media**（L2）；§2.1 F 域「待验」销账 |
| 相册评论 | 确切名 **do_group_album_comment**（L1）；同上销账；另 cancel_group_album_media_like 补登记 |
| 批量踢人 | 确切名 **set_group_kick_members**（L2）；§2.5 待验销账 |
| 群签到名单 | 注册表键名即 **get_group_signed_list**，定名落定 |
| 删群文件夹 | 注册表键名即 **delete_group_folder**，定名落定 |
| 私聊文件 URL | 注册表键名即 **get_private_file_url**，定名落定 |

## 4. 未决项（待验清单与验证路径）

| 项 | 状态 | 验证路径 / 责任阶段 |
|---|---|---|
| get_group_todo | **维持待验**：bundle 无登记 + 实调不支持（双证据缺失）；待办族写三名在册、查询名无着落——可能不存在或另有机制 | NapCat 升级时重跑三方对账；值守在场时 WebUI Debug 全表浏览复核 |
| .get_word_slices schema | 在册已证实；schema 待验（N0 纪律下非 get_* 前缀不实调） | 值守 WebUI Debug schema 浏览；或 N1 落 Fake 契约时以 bundle/文档证据定 schema |
| 闪传族 schema（create_flash_task、send_flash_msg、download_fileset 及 get 系内容） | 存在已证实（在册 + get 系必填参数复证）；完整 schema 待验 | N5 Fake 契约；写类必要时走 §7.3 第 3 级影子账号实测（另行立项、须批准） |
| 流式族 schema（5 名） | 在册已证实；schema 待验 | 同上，N5 |
| 在线文件写动作 schema（send_online_file/folder、receive/refuse/cancel_online_file） | 在册已证实；schema 待验 | 同上，N5 |
| delete_unidirectional_friend | **维持挂起**：官方 ❌ × Apifox 有列 × bundle 仅注释行；证据倾向不支持 | NapCat 升级复核（方案 §2.5 口径不变） |
| trans/move/rename 三名主名映射 | 三名并存注册已证实；主名映射未定 | N5 schema 核对 |
| 补登记 39 项与单源项（_mark_all_as_read、translate_en2zh、相册单源 2 名等）的 schema | 在册已证实；schema 待验 | 各自 owner_stage 落 Fake 契约时核对（登记表 notes 已逐项标注） |
| L3 族 schema（get_rkey 系、get_clientkey、send_packet） | 在册已证实；schema 待验；**未实调**（凭证纪律） | N6 受控解锁实现时以 bundle/文档证据核对 |

## 5. 登记表工件计数（固化口径）

`companion/adapters/action_registry.py` 共 **191 条** = 在册 182 + 挂起 9：

- 在册 182 的处置：registered **174**、excluded_internal（内部/测试名）4、
  ops_only（bot_exit、set_restart，只走 runbook）2、deprecated（频道族
  get_guild_list、get_guild_service_profile）2；
- registered 174 中别名条目 7（move_group_file、rename_group_file、
  get_group_ignore_add_request、.ocr_image、set_group_sign、ArkSharePeer、
  ArkShareGroup），**canonical 功能动作 = 167**；
- 挂起 9：not_supported 4（set_group_anonymous、set_group_anonymous_ban、
  qidian_get_account_info、reload_event_filter）、pending_verification 2
  （delete_unidirectional_friend、get_group_todo）、nonexistent 1
  （send_red_packet，发红包无 API）、not_registered 2（reboot_normal、
  upload_forward_msg，注释行独有）；
- 状态五元组汇总：existence verified 182 / absent 9；schema verified
  **102** / unverified 89（口径：verified = 官方文档有参数/返回说明且
  bundle 在册的双源项，或官方仓库 action 类证实项；unverified = 仅
  在册/单源、内容待验；探针不升级此档）；implemented 与
  behavior_verified 全 False，按方案 §1.1 计数口径 **已接入 = 0**
  （门面自 N1 起按 owner_stage 落地）；挂起 9 条的责任阶段均为
  **N6**（登记表终审时逐项复核处置），复核点另见各条 notes；
- 方案 §2.4 的「登记约 150 项」为登记表行数估算，自 N0 起以本节注册表
  对象口径的精确计数为准。

## 6. 覆盖矩阵（独立交付物）

- `docs/napcat-event-coverage.md`（数据：`tests/fixtures/napcat/event_coverage.json`，
  32 行）：notify 的 lucky_king/honor 标**单源存疑**；bundle 独有
  online_file_receive/online_file_send/gray_tip/group_name 已登记；
  profile_like/input_status 由 SDK 索引升级为 bundle 证实；
  message_sent 方向契约单列。自审记录见该文档。
- `docs/napcat-segment-coverage.md`（数据：`tests/fixtures/napcat/segment_coverage.json`，
  23 行）：flashtransfer 与在线文件段标待验，其余与方案 §2.2 一致。
  自审记录见该文档。

## 7. 本阶段文件清单

新增：`companion/adapters/action_registry.py`（纯数据骨架）、
`tests/fixtures/napcat/{bundle_registry_snapshot,report_action_set,event_coverage,segment_coverage}.json`、
`tests/scripts_n0/extract_bundle_registry.py`、
`tests/test_napcat_registry_n0.py`、
`docs/napcat-verification-n0.md`、`docs/napcat-event-coverage.md`、
`docs/napcat-segment-coverage.md`，及 CHANGELOG 一条。
未改动任何既有运行时代码、配置与测试。

## 8. Codex Review 记录（本阶段一次合并调用，gpt-6.1-sol / high）

结论：无 P1；P2 共 5 条，裁决如下（修复提交紧随本记录）：

1. **测试不校验逐条元数据**（变异 delete_msg 等级/层级/阶段、message_sent
   改入私聊链均可全绿）——**Partially Accept**：全表复制进测试是维护
   陷阱，不采；采要害条目独立期望表（35 项，含 L2/L3 风险面、文档冲突、
   定名、别名 canonical、E2 族与各域代表）+ 矩阵关键行方向/处置断言。
2. **integrated_count 不归一别名**（canonical + 2 别名全落地会数 3）——
   **Accept**：改为只数 canonical registered 条目，并加合成用例
   （三件套数 1、别名单独数 0、ops_only 数 0）。
3. **get_ai_characters 的 schema 标记与本文档探针措辞矛盾**——
   **Partially Accept**：标记按双源口径（官方文档有 schema）成立；
   文档探针结论措辞已修正，schema 两档定义已写明于本文档 §5 与注册表
   模块 docstring，102 计数口径自洽。
4. **挂起项无责任阶段**——**Accept**：9 条挂起的 owner_stage 统一为
   N6（登记表终审逐项复核），测试加断言。
5. **探针替代 §7.3 字面的 WebUI Debug 浏览**——**Partially Accept**：
   探针为本阶段执行纪律明确许可的只读替代（WebUI 登录属值守动作）；
   偏离已在 §2 显式声明，schema 待验状态不因探针升级，WebUI Debug
   schema 浏览记为未欠事项保留在待验路径中。

其余四类（计数算术、定名与处置口径、运行时洁净、矩阵内容）审方明确
确认无问题；审方独立重抽 bundle 与夹具逐项一致。

## 9. N5 销账追记（2026-10-31，分支 ai/qqbot-napcat-n5，只追加不改原文）

本节为 N5 阶段对上文 §4 未决项中归 N5 各行的销账追记，原文各节
口径保留不动；完整阶段记录见 `docs/napcat-files-media-n5.md`，
字段级证据见 `docs/napcat-schema-evidence-n5.md`。

- **闪传族 schema**：已销账。payload 字段级证据定稿，
  create_flash_task / send_flash_msg / download_fileset 接通，
  get 系（get_flash_file_list、get_flash_file_url、get_fileset_info、
  get_fileset_id）升 schema=verified。例外：get_share_link 不在
  N5 证据集内，仍待验、留 N6。
- **流式族 schema（5 名）**：已销账。upload_file_stream /
  download_file_stream / download_file_image_stream /
  download_file_record_stream 定稿并接通；test_download_stream
  经 handler 证据定性为测试专用合成流，不接门面、owner_stage
  改 N6 终审处置。
- **在线文件写动作 schema**：已销账。send_online_file /
  send_online_folder / receive_online_file / refuse_online_file /
  cancel_online_file 定稿并接通（payloadExample 与 schema 矛盾处
  以 schema 为准）；读动作 get_online_file_msg 升 verified
  （其既有门面参数形态与证据字段的差异记于 N5 阶段文档 §5）。
- **trans/move/rename 三名主名映射**：已销账并纠正 N0 别名
  登记——bundle schema 证据显示三者为独立动作、字段互不相同，
  move/rename 改独立 canonical（别名 7 → 5、canonical
  167 → 169）；登记表与本文件 §5 的 N0 时点计数以本追记为准
  更新为：schema verified 102 → 130、已接入计数 132 → 160。
- **相册单源 2 名 schema**：已销账。get_qun_album_list /
  get_group_album_media_list 经 bundle payloadSchema 定稿升
  verified；F 域其余待验名（del_group_album_media、
  do_group_album_comment、cancel_group_album_media_like）一并
  定稿接通。
- **段待验 2 类**（§6 覆盖矩阵）：flashtransfer / onlinefile
  （正名：无下划线）已定稿为 receive-only（可收不可发），
  矩阵已同步更新，详见 `docs/napcat-segment-coverage.md` 的
  N5 落地附记。
- 仍保留待验者：get_share_link schema、28 个动作 returnSchema
  为 p.Any 的 return 字段级契约、get_group_files_by_folder 的
  folder_id 口径差异（网关维持更严必填）——原因与后续路径见
  N5 阶段文档 §5/§6。

## N6 终审销账追记（2026-10-31）

N5 追记留给 N6 的待验项与本文件 §4 待验表，本阶段终审如下
（证据：`docs/napcat-schema-evidence-n6.md`；总收口：
`docs/napcat-integration-closeout.md`）：

- **get_share_link schema**：已销账。bundle 动作类证据补抽，
  payload 定稿（fileset_id 必填）升 verified；其 return 为
  p.Any，并入下一项口径。
- **p.Any return 字段级契约（28 + get_share_link = 29）**：终审
  裁决不立约——维持三态 + 平台 data 透传（残留 R-N1）。理由：
  bundle 本身无字段定义，字段表只能来自实调采样，失真风险
  高于收益；出现产品级字段依赖时按单动作立项。
- **folder_id 口径差异**：终审维持网关更严必填（残留 R-N2，
  已知差异定案，不再算待验）。
- **`.get_word_slices`（§4 表「在册已证实、schema 待验」）**：
  **证据升级、结论推翻**。N6 动作类抽取证实注册表虽有键，但
  全 bundle 无 handler 类（键名仅出现 1 次）；登记表处置自
  registered 改判 excluded_internal、门面下线。本文件 §2/§3
  的「在册」表述以本追记为准修正为「注册表有键、无实现」。
- **`get_group_todo`**：终审维持待验（pending_verification）。
  N6 抽取复证不在注册表（found=false），与 N0 实调「不支持」
  互证；验证路径不变（升级重对账 / 值守 WebUI Debug 全表浏览），
  终局处置即挂起待路径证实，不静默关闭。
- **`delete_unidirectional_friend`**：终审维持挂起。三方证据
  本轮无变化（官方 ❌ × Apifox 有列 × bundle 仅注释行），
  路径同上（升级复核）。
- **WebUI Debug 值守浏览**：仍未执行（无人值守纪律下不做），
  其取证职能已被 bundle 静态抽取覆盖大半；剩余用途收敛为
  上两条挂起项的备选验证路径，留待有值守窗口时一并处理。
- 至此本文件待验表全部条目均有终局结论：销账、定案或维持
  挂起并写明路径，无无主条目。登记表终态：191 条、registered
  172（canonical 167）、schema verified 138、已接入 167。
