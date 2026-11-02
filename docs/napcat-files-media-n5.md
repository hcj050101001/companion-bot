# NapCat 全接口接入 N5：文件与媒体域（阶段记录）

阶段：NapCat 全接口接入 **N5（文件媒体域）**，分支
`ai/qqbot-napcat-n5`。本阶段在 Worker A 的核心契约
（`ResourceResolver` 完整版、`FileTaskManager`/`stream_upload`、
`ActionCircuitBreaker`，`tests/test_file_contracts_n5.py` 54 测）与
Worker S 的 schema 证据（`docs/napcat-schema-evidence-n5.md` +
`tests/fixtures/napcat/bundle_action_schemas.json`，范围内 40 个动作
payload 字段级证据全部定稿）之上，把文件与媒体域接进
gateway / 门面 / 登记表 / 段模型，并以
`tests/test_files_media_n5.py`（130 测）作集成契约。

纪律：全程 FakeTransport + tmp_path 本地临时文件；不部署、不重启、
不发真实消息、不真实上传下载、不改生产 config、不动 v0.9.0 tag。

## 1. 接通清单（28 canonical，integrated 132 → 160）

门面参数严格按证据 JSON 字段表：可选字段 None 不进 params；带
schema default 的字段（`upload_file`、`file_retention`、
`file_count` 等）同样以 None 表示用平台默认、不代填。

- **E 写 9**（`FileActions`）：`upload_private_file`、`download_file`、
  `upload_group_file`、`delete_group_file`（L2）、
  `create_group_file_folder`、`delete_group_folder`（L2）、
  `trans_group_file`、`move_group_file`、`rename_group_file`。
- **E2 写 12**（`FileActions`）：`create_flash_task`、`send_flash_msg`、
  `download_fileset`、`send_online_file`、`send_online_folder`、
  `receive_online_file`、`refuse_online_file`、`cancel_online_file`、
  `upload_file_stream`、`download_file_stream`、
  `download_file_image_stream`、`download_file_record_stream`。
- **F 写 7**（新增 `AlbumActions`，挂 `ActionsBundle.album`）：
  `upload_image_to_qun_album`、`upload_images_to_qun_album`、
  `upload_video_to_qun_album`、`set_group_album_media_like`、
  `cancel_group_album_media_like`、`del_group_album_media`（L2）、
  `do_group_album_comment`。`get_qun_album_list` /
  `get_group_album_media_list` 已在 `FileActions`，本阶段不动。

每动作 Fake 契约三态（成功 ok / 平台拒绝 rejected / 传输异常
ambiguous）+ 动作名与 params 逐字断言，共 28×3 参数化用例。

**网关侧**：

- `_PARAM_RULES` 新增 **24 条**（仅必填：optional=False 且无 schema
  default 者）。`download_file` 与三个 `download_*_stream` 全字段
  可选、无必填，不设条目；`do_group_album_comment` 的类级 check()
  约束（lloc 与 batch_id 至少一个）由门面层拒绝双缺
  （`invalid_params`，不调网关）——schema 本身未表达此约束。
- **L2 删除三动作**（`delete_group_file`、`delete_group_folder`、
  `del_group_album_media`）入 `_L2_TOKEN_REQUIRED`（不可逆档，与
  `set_group_leave`/`delete_friend` 同口径）与 `_L2_ADMIN_ACTIONS`
  （三者证据字段均含 group_id，可按群复核机器人管理员角色；
  `del_group_album_media` 的字段表为 group_id/album_id/lloc，含
  group_id，故同入两集——不是只有令牌档），并在
  `_TARGET_KIND_BY_ACTION` 声明 group 目标派生。授权缺失 / 越界 /
  无令牌 simulated / 令牌错配 / 角色未核实 fail closed / 角色不足
  均有参数化断言。
- **相册熔断**：`ActionGateway` 新增可选参数 `circuit_breaker`
  （默认构造 `ActionCircuitBreaker(threshold=3, cooldown_seconds=300)`）
  与 `circuit_actions`（默认由登记表 domain="F" 的 canonical 动作
  派生，共 9 个）。派发前以 `state()` 只读预检 open 状态 →
  `rejected` + `gate="circuit_open"` + `logger.warning` 告警行
  （含 action 名与状态；预检不消费 half-open 试探名额、不烧确认
  令牌）；真正派发点（`_dispatch`）才调 `allow()` 并按派发后结果
  记账——成功 `record_success`，平台 rejected / ambiguous（含传输
  异常归一）记失败，**网关门禁拒绝一律不计**。非 F 域行为逐字不变
  （既有 gateway 测试全绿，另有 B 域连续失败不触发熔断的断言）。
- **落盘审计**：文件域动作（登记表 domain ∈ {E, E2, F} 的
  canonical）审计行尾追加 ` resources=<digest,...> size=<n|none>`：
  资源键（file/files/file_path/folder_path/url/base64/path/file_id/
  fileset_id/file_set_id/image/video/thumb_path/chunk_data，以证据
  字段为准）的值经 sha256 前 16 位摘要（与 resources.py digest
  口径一致）再经 `_audit_token` 清洗，绝不落路径/URL 原文；声明
  大小取 `file_size`/`size` 的 int 值。非文件域审计行逐字不变。
  caplog 断言：含摘要与结果码、不含真实路径/签名参数。
- **ResourceResolver 接线**：`ActionGateway(resource_resolver=...)`
  可选注入；注入时在步骤 d 对文件域动作的资源值逐个
  `resolver.resolve`（本地路径根目录/扩展名、base64 估算大小——
  `chunk_data`/`base64` 键的裸 base64 按 `base64://` 形态估算、
  声明大小），拒绝回 `gate="resource_rejected"`；未注入时行为与
  现状逐字一致。集成测试以 tmp_path 真文件证明：resolver 根更窄
  时根外拒、扩展名不合规拒、超 `max_bytes` 的 base64 块拒、合规
  本地文件放行派发、未注入时 png 放行（行为不变）。
- **任务契约示范**（不改 `file_tasks.py` API）：`FileTaskManager.run`
  包裹门面 `upload_group_file`（Fake 成功 → succeeded/ok）与
  `download_file`（慢 Fake + timeout → timed_out/timeout）；
  `stream_upload` 驱动门面 `upload_file_stream` 分 3 块 invoke，
  进度断言 `[None, 1/3, 2/3] → 1.0`、chunk_index 序 `[0,1,2]`；
  中途 `cancel()` 后后续块零 invoke、任务落 cancelled。

## 2. 协调定案 (a)–(e) 的落地

**(a) schema 升级口径**：证据中「可定稿」的动作按方案/N0 两档口径
中「官方仓库 action 类证实」一档（bundle payloadSchema 即官方
action 类自带 schema）升 `verified`，notes 注明证据指针
（`docs/napcat-schema-evidence-n5.md`）。共 **28 条**升档
（新接写动作 19 + 已接 L0 读动作 8：`get_flash_file_list`、
`get_flash_file_url`、`get_fileset_info`、`get_fileset_id`、
`get_online_file_msg`、`get_qun_album_list`、
`get_group_album_media_list`、`get_private_file_url` +
`test_download_stream` 1），登记表 verified 总数 102 → **130**。
证据集 40 个动作中 **28 个 returnSchema 为 p.Any**、无字段级
return 定义——这是 bundle 定义上限，不影响 payload schema 档；
这 28 个动作的 return 字段级契约如需建立，证据路径只能是官方
文档或受控实测，留 N6 视需要立项。

**(b) trans/move/rename 定案**：三者为独立 canonical（trans 仅
group_id+file_id；move 另加 current/target_parent_directory；
rename 另加 current_parent_directory+new_name）。登记表
`move_group_file`/`rename_group_file` 的 `alias_of` 清空、改独立
条目并升 verified；gateway 别名表随登记表自动派生（有
`resolve_canonical_name` 自证断言与三名各自派发断言）。
`tests/test_napcat_registry_n0.py` 受影响断言已同步更新并在注释中
写明这是 N5 按方案分工的定案纠正、非口径漂移：别名 7 → **5**、
canonical 167 → **169**、`integrated_count` 别名归一示例改用
send_group_sign/set_group_sign 对（三名现计 3）。

**(c) test_download_stream 定性**：测试专用合成流（不读 URL、不调
核心 API、只产合成文本块并可按入参触发测试错误，证据见证据文档
§3）。不接门面，登记表 `implemented` 保持 False，notes 已更新
定性结论，`owner_stage` 改 **N6**（终审处置）。

**(d) get_group_files_by_folder 的 folder_id 冲突**：bundle 侧
folder_id 为 Optional、`_PARAM_RULES` 列为必填——网关维持更严
口径、不改行为；本条记为**已知差异**，留待 NapCat 升级复核时
连同 file_count（bundle 带 default 50、网关未提）一并复议。

**(e) 两类段 receive-only 定稿**：平台段字面量分别为
`flashtransfer`（data 仅 fileSetId 必填）与 **`onlinefile`（无
下划线**；data 五字段 msgId/elementId/fileName/fileSize/isDir 全
必填，fileSize 为 String、isDir 为 Boolean）。NapCat 的 ob11→raw
转换器对两者均为空实现（原文注释「不需要支持发送」），故只建模
为入站可收、出站拒绝发送：登记于 `KNOWN_RAW_SEGMENT_TYPES`
（新增 `RECEIVE_ONLY_SEGMENT_TYPES` 常量），入站解析产出
RawSegment 并校验 data 关键字段（缺失/类型不对即整条消息判
malformed，fail closed）；出站序列化（CQ 与 segment 数组两路，
含 NodeSegment 内容递归）遇此两类返回 None——与既有不可序列化
段同一拒绝口径（发送前 rejected）。`online_file` 正名注记：
该字面量在 bundle 中无段证据，仅见于动作名/事件名。
`tests/fixtures/napcat/segment_coverage.json` 与
`docs/napcat-segment-coverage.md` 已同步（direction=inbound、
disposition=receive_only、verification=bundle_confirmed），
N0 段矩阵断言同步更新。

## 3. N0 §4 待验表归 N5 项的销账结果

| N0 待验项 | N5 销账结果 |
|---|---|
| 闪传族 schema（create_flash_task、send_flash_msg、download_fileset 及 get 系内容） | **已销账**：payload 字段级证据定稿（证据文档 §1），写 3 动作接通、get 系 4 动作（get_flash_file_list/url、get_fileset_info/id）升 verified。例外：`get_share_link` 不在本次证据集内，其 schema 仍 unverified（见 §5）。 |
| 流式族 schema（5 名） | **已销账（4 接通 + 1 定性）**：upload/download/image/record 四动作 payload 定稿并接通；`test_download_stream` 定性为测试专用合成流，转 N6 终审（定案 c）。 |
| 在线文件写动作 schema（send_online_file/folder、receive/refuse/cancel_online_file） | **已销账**：payload 定稿（receive/refuse 的 payloadExample 与 schema 矛盾，以 schema + handler 为准，示例作废）；5 写动作接通，读动作 `get_online_file_msg` 升 verified。注：既有门面 `get_online_file_msg(message_id=...)` 的参数形态与证据字段（user_id 必填）不一致——协调者已裁决在本阶段按证据修正签名（见 §5），销账。 |
| trans/move/rename 三名主名映射 | **已销账**：三独立 canonical 定案（定案 b）。 |
| 相册单源 2 名（get_qun_album_list、get_group_album_media_list）等 schema | **已销账**：bundle payloadSchema 证据定稿、升 verified；F 域其余待验名（del_group_album_media、do_group_album_comment、cancel_group_album_media_like）同样定稿并接通。 |

## 4. OCR 整合形态

`MediaActions.ocr_image` 门面（N1 已有）归入本阶段契约
（`tests/test_files_media_n5.py`）：经网关 Fake 三态（ok / 平台
拒绝 / 传输异常 ambiguous）+ 参数逐字断言；其图片资源经网关
既有资源边界参与校验——`image` 键在网关 `_RESOURCE_PARAM_KEYS`
内，根外绝对路径派发前 `resource_rejected`、根内文件与 https
URL 放行。形态定性：**查询类（L0）动作**，登记表 schema 本已
verified（证据经父类继承定稿，证据文档 §5.4 注明继承来源一跳）；
ResourceResolver 接线按文件域（E/E2/F）口径不含 D 域，OCR 不经
resolver，此点为口径声明而非缺口。

## 5. 仍保留待验 / 已知差异（不臆补）

- `get_share_link`（E2）：不在 Worker S 的 40 动作证据集内，
  schema 维持 unverified，留 N6 或下次证据抽取时补。
- 28 个动作 return 为 p.Any（见定案 a）：无字段级 return 契约，
  门面/Fake 只断言三态与平台 data 透传，不臆造返回字段。
- `get_group_files_by_folder` folder_id / file_count 差异（定案 d）。
- `get_online_file_msg` 既有门面参数（message_id）与证据字段
  （user_id 必填）不一致：**协调者裁决（N5 内修正）**——证据
  确凿（payloadSchema 唯一必填 user_id、handler 只读 user_id、
  payloadExample 同），N1 签名属 schema 待验期的臆测，对真实
  平台必然 400；已在本阶段改门面签名为 `user_id`、gateway
  `_PARAM_RULES` 补该动作必填闸、test_facades_n1 一处用例同步
  更新（均注定案来源）。
- schema 未表达的约束一律不臆造必填：download_file 的
  url/base64 二选一、download_*_stream 的 file/file_id 二选一、
  send_flash_msg 的 user_id/group_id 目标二选一、
  create_group_file_folder 的 folder_name/name 至少一个、
  delete_group_folder 的 folder_id/folder 至少一个。
- `create_flash_task.validitySeconds`：description 称默认
  1209600 但 schema 无 default 属性，按「无 schema 默认值」
  处理（门面不代填）。
- `send_online_file`/`send_online_folder`/`send_flash_msg` 登记
  kind=message：经 FileActions 门面调用时不走投递句柄纪律
  （与 MessagingActions 的句柄门面不同），网关额度路径在注入
  SendQuota 时按既有「无 token 自行预留」口径记账；运行时默认
  关（L1 default_enabled=False）不变。

## 6. 留给 N6 的清单

- `test_download_stream` 终审处置（owner_stage 已改 N6）：
  维持登记为测试专用、或在终审时改判 excluded_internal——
  由 N6 登记表终审裁决。
- L3 族（get_cookies/get_csrf_token/get_credentials/get_clientkey/
  get_rkey 系/send_packet）受控解锁与 schema 核对（原 N6 范围）。
- `get_share_link` schema 证据补抽；28 个 p.Any 动作的 return
  字段级契约（如产品需要，官方文档或受控实测另行立项）。
- folder_id 已知差异的终审复议（`get_online_file_msg` 参数差异
  已于 N5 内裁决修正，不再留 N6）。
- bot_exit / set_restart 等 ops_only 与其余挂起项的终审（原口径）。

## 7. 测试证据

- 新增 `tests/test_files_media_n5.py`：**130 测全绿**（28 动作
  三态 84 + L2 删除授权链参数化 18 + 熔断 7 + 审计 3 + resolver
  4 + 任务契约 4 + OCR 2 + 段 4 + 参数规则/别名等 4）。
- 登记表断言改动（`tests/test_napcat_registry_n0.py`）：canonical
  167→169、integrated 132→160、verified 102→130、E2 未定稿集
  收敛为 {get_share_link}、N5_LANDED 名单新增、EXPECTED_METADATA
  6 项更新/新增、段矩阵两行改判——每处均注 N5 定案来源。
- 全套与 lint：见阶段汇报（pytest + ruff 收尾自检）。

## 8. Review 记录（Claude Code，DeepSeek flash / max）

审查方说明：用户 2026-10-31 12:51 确认 Codex 额度耗尽并指示
后续 Review 全部改用 Claude Code——本阶段按此执行（降级原因
即用户确认的 Codex 额度耗尽，属 Harness Policy 既定降级条件）。
一次合并 Review 共 12 条 findings（P1×1、P2×3、P3×8），裁决
Accept 9 / Partially Accept 3 / Reject 0，成立项全部修复并带
13 条回归（`tests/test_review_fixes_n5.py`），修复后全套
1423 passed、Ruff 全过：

- **P1-1 Accept**：half-open 试探被取消/超时（CancelledError
  穿透传输层 Exception 归一）时 probing 永不结清、动作熔断
  死锁。修：circuit 增 `abandon_probe`（按失败结清、刷新
  冷却），gateway 派发以 BaseException 兜底结清试探名额。
- **P2-1 Accept**：派发点 circuit_open 发生在令牌消费/账本
  占位之后（烧令牌 + 账本误记 done 不可重试）。修：i2b 在
  消费前复检 open 状态；派发点拒绝改记 aborted（同
  operation_id 可重试）。残余：消费后状态变化的极窄竞态内
  令牌仍会被烧，须签发方重签（已注代码口径）。
- **P2-2 Accept**：新资源键（file_path/folder_path/thumb_path/
  files）不在网关资源键集，裸本地路径可直通。修：补入
  `_RESOURCE_PARAM_KEYS` 且列表元素逐个校验。resolver 默认
  注入与否属部署配置决策，未改既有构造点（生产装配仍按
  resource_roots 口径）。
- **P2-3 Partially Accept**：resolver 接线 fail-open 两处。
  修（网关侧）：资源键下 dict fail closed、嵌套列表递归；
  路径语义键解析为 file_id 分类时拒绝。resolver 本体的
  「相对串归 file_id」分类是其文档化通用口径（file_id 本身
  可含斜杠），不在本体收紧、由网关按键语义收口。
- **P3-1 Partially Accept**：cancel 取消的是承载 run 的调用
  方任务、与 docstring「pending 直接落终态」表述不符。修：
  docstring 写明落账分工与 create_task 隔离口径，行为不变
  （契约阶段口径，内部独立 Task 化留后续按需）。
- **P3-2 Accept**：跨任务同路径临时文件配额重复计账 + 先
  cleanup 误删他人文件。修：引用计数（配额只记一份、最后
  一个引用 cleanup 才删）。
- **P3-3 Accept**：stream_upload 缺 RUNNING 重入保护，已补
  （与 run() 对齐）。
- **P3-4 Accept**：熔断迟到结果与在飞试探混淆。修：试探
  世代号——`allow()` 发放时自增，记账携带世代，不匹配的
  迟到成功/失败不影响试探；gateway 记账按持有/迟到分派
  世代参数。
- **P3-5 Accept**：孤立代理项让审计摘要 encode 在门禁前
  抛错、无审计无拒绝。修：`backslashreplace` 编码 + 回归。
- **P3-6 Accept**：ResolvedResource.ref / FileTask.temp_paths
  默认 repr 泄漏面。修：`field(repr=False)`。
- **P3-7 Partially Accept**：do_group_album_comment 门面本地
  拒绝绕过审计。修：补同格式审计行（约束本身仍在门面，
  网关参数规则无法表达二选一）。
- **P3-8 Accept**：RawSegment("node"/"forward") 嵌套 content
  可夹带 receive-only 段。修：序列化前递归扫描，命中即拒。
- 审方残余风险（接受、记 N6 终审清单）：`file://` 不做百分
  号解码的校验/解释错位面；signed_url 启发式漏标面（当前
  无消费方）；网关路径未接 resolver 的会话绑定/过期（契约
  阶段定位，运行时绑定留后续接线）；审计摘要条数已加上限
  20（本次顺带修）。

## N6 Review 修复追记（2026-10-31）

本阶段落地的 E2 读族五名（get_flash_file_list /
get_flash_file_url / get_fileset_info / get_fileset_id /
get_share_link）的门面签名在 N6 终审 Review（Claude Code，
P2-1）被发现与本阶段自产的 bundle schema 证据不符：门面沿用
N1 时的臆测名 file_set_id/file_id（且缺 get_fileset_id 的
share_code），实调必回 400——Fake 契约以无参调用 + 默认 ok
响应掩盖了该缺陷，属本阶段「behavior_verified」口径的实质
欠账。已在 N6 按证据改签名、补网关 _PARAM_RULES 必填与夹具
对齐契约测试（tests/test_facades_n1.py），修复后五名的接入
口径与证据一致。本文件正文的当时记录不改，以本追记为准。
