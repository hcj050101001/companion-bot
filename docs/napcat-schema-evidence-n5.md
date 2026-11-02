# NapCat schema 字段级证据（N5）

阶段：NapCat 全接口接入 **N5（文件媒体域）** 的证据工件。基线 bundle：
本机 NapCat **4.18.41** 的 `napcat.mjs`（MD5
`7fdb4903c8867e7db0b3345ad226c805`，3,134,858 字节，与 N0 快照
`tests/fixtures/napcat/bundle_registry_snapshot.json` 的 `bundle_md5`
一致，脚本自检 `md5_matches_n0_snapshot=true`）。

工件：

- 抽取脚本 `tests/scripts_n5/extract_bundle_schemas.py`（只读解析 bundle，
  不联网、不调用真实 NapCat；输出无时间戳，连跑两遍 `diff` 逐字节一致）；
- 证据 JSON `tests/fixtures/napcat/bundle_action_schemas.json`（下文所有
  结论均回指该文件：`actions.<动作>.payload_fields` /
  `return_fields` / `return_schema` / `notes`、
  `test_download_stream_analysis`、`segment_evidence`）。

方法：注册表 `const re = {...}` 反查动作键名 → `actionName = re.<Key>`
定位动作类 → 解析类内（或父类链）`payloadSchema` 的 TypeBox 定义原文
逐字段抽取（类型 / Optional / default / description）→ 以类 `_handle`
的参数字段访问列表（`handler_field_accesses`）作旁证对账。

**范围计数说明**：任务标题称 41 个，但其枚举清单逐项数得 **40 个**
动作名，本工件按枚举全部抽取（`summary.requested=40, found=40`），
不存在可臆补的第 41 个名字。

**结论口径**：「可定稿」= payload schema 的字段级证据完整——每个字段的
名称、类型、是否 Optional、default、description 均来自 bundle 内
TypeBox 定义原文，且 `_handle` 字段访问与之不矛盾。「仍待验」= 字段级
证据缺失或互相矛盾到无法定字段表。本次 40 个动作的 payload 全部达到
可定稿标准（**定稿 40 / 待验 0**）；个别动作的示例（payloadExample）
与 schema 矛盾、return 为 `Any` 等问题单列在 §5，不改变 payload 定稿
结论，但主线落契约时须按 §5 口径处理。

## 1. 逐动作结论表

字段标注：`名:类型`；† = 非 Optional 但带 default（bundle 原值照录）；
return 列中 `Any（描述）` 表示 bundle 的 returnSchema 本身就是
`p.Any({description})`，无字段级定义（见 §5.1）。

| 动作 | 结论 | 必填字段 | 可选字段 | return 证据 |
|---|---|---|---|---|
| upload_private_file | 可定稿 | user_id:String、file:String、name:String、upload_file:Boolean†(default `!0`) | — | Object：file_id:Union[String, Null] |
| download_file | 可定稿 | —（四字段全可选，见 §5.3） | url:String、base64:String、name:String、headers:Union[String, Array\<String\>] | Object（下载结果）：file:String |
| upload_group_file | 可定稿 | group_id:String、file:String、name:String、upload_file:Boolean†(default `!0`) | folder:String、folder_id:String | Object：file_id:Union[String, Null] |
| delete_group_file | 可定稿 | group_id:String、file_id:String | bus_id:Number、busid:Number | Any（删除结果） |
| create_group_file_folder | 可定稿 | group_id:String | folder_name:String、name:String（同义二选一，schema 均为可选，见 §5.3） | Object（创建文件夹结果）：result:Any、groupItem:Any |
| delete_group_folder | 可定稿 | group_id:String | folder_id:String、folder:String（同义，均为可选） | Any（删除结果） |
| trans_group_file | 可定稿 | group_id:String、file_id:String | — | Object（转发文件结果）：ok:Boolean |
| move_group_file | 可定稿 | group_id:String、file_id:String、current_parent_directory:String、target_parent_directory:String | — | Object（移动文件结果）：ok:Boolean |
| rename_group_file | 可定稿 | group_id:String、file_id:String、current_parent_directory:String、new_name:String | — | Object（重命名文件结果）：ok:Boolean |
| get_group_file_url | 可定稿（对照组，与网关一致，见 §4） | group_id:String、file_id:String | — | Object（群文件URL信息）：url:String（Optional） |
| create_flash_task | 可定稿 | files:Union[Array\<String\>, String] | name:String、thumb_path:String、validitySeconds:Number（description 称默认 1209600，但 schema 无 default 属性，见 §5.2） | Any（任务创建结果） |
| send_flash_msg | 可定稿 | fileset_id:String | user_id:String、group_id:String（目标二选一未在 schema 表达，见 §5.3） | Any（发送结果） |
| download_fileset | 可定稿 | fileset_id:String | — | Any（下载结果） |
| get_flash_file_list | 可定稿 | fileset_id:String | — | Any（文件列表） |
| get_flash_file_url | 可定稿 | fileset_id:String | file_name:String、file_index:Number | Any（文件下载链接） |
| get_fileset_info | 可定稿 | fileset_id:String | — | Any（文件集信息） |
| get_fileset_id | 可定稿 | share_code:String | — | Object：fileset_id:String |
| send_online_file | 可定稿 | user_id:String、file_path:String | file_name:String | Any（发送结果） |
| send_online_folder | 可定稿 | user_id:String、folder_path:String | folder_name:String | Any（发送结果） |
| get_online_file_msg | 可定稿 | user_id:String | — | Any（在线文件消息列表） |
| receive_online_file | 可定稿（示例与 schema 矛盾，以 schema 为准，见 §5.2） | user_id:String、msg_id:String、element_id:String | — | Any（接收结果） |
| refuse_online_file | 可定稿（示例漏 element_id，以 schema 为准，见 §5.2） | user_id:String、msg_id:String、element_id:String | — | Any（拒绝结果） |
| cancel_online_file | 可定稿 | user_id:String、msg_id:String | — | Any（取消结果） |
| upload_file_stream | 可定稿 | stream_id:String、file_retention:Number†(default `300 * 1e3`) | chunk_data:String、chunk_index:Number、total_chunks:Number、file_size:Number、expected_sha256:String、is_complete:Boolean、filename:String、reset:Boolean、verify_only:Boolean | Any（上传结果 (流式)） |
| download_file_stream | 可定稿 | —（三字段全可选，见 §5.3） | file:String、file_id:String、chunk_size:Number(default `64 * 1024`) | Any（下载结果 (流式)） |
| download_file_image_stream | 可定稿 | —（同上） | file:String、file_id:String、chunk_size:Number(default `64 * 1024`) | Any（下载结果 (流式)） |
| download_file_record_stream | 可定稿 | —（四字段全可选） | file:String、file_id:String、chunk_size:Number(default `64 * 1024`)、out_format:String | Any（下载结果 (流式)） |
| test_download_stream | 可定稿（schema 完整；行为定性为测试专用，见 §3） | — | error:Boolean(default `!1`) | Any（测试流数据） |
| upload_image_to_qun_album | 可定稿 | group_id:String、album_id:String、album_name:String、file:String | — | Any（上传结果） |
| upload_images_to_qun_album | 可定稿 | group_id:String、album_id:String、album_name:String、files:Array\<String\> | — | Any（上传结果） |
| upload_video_to_qun_album | 可定稿 | group_id:String、album_id:String、album_name:String、file:String | — | Any（上传结果） |
| set_group_album_media_like | 可定稿 | group_id:String、album_id:String、batch_id:String | lloc:String | Any（操作结果，与 cancel 共用同一 schema 变量 NL/PL） |
| cancel_group_album_media_like | 可定稿 | group_id:String、album_id:String、batch_id:String | lloc:String | Any（操作结果，同上共用 NL/PL） |
| del_group_album_media | 可定稿 | group_id:String、album_id:String、lloc:String | — | Any（删除结果） |
| do_group_album_comment | 可定稿（另有 check() 约束，见 §5.3） | group_id:String、album_id:String、content:String | lloc:String、batch_id:String | Any（评论结果） |
| get_qun_album_list | 可定稿 | group_id:String | attach_info:String(default `""`) | Object（群相册列表响应）：album_list:Array\<Any\>、attach_info:String、has_more:Boolean |
| get_group_album_media_list | 可定稿 | group_id:String、album_id:String | attach_info:String(default `""`) | Any（相册媒体列表） |
| ocr_image | 可定稿（证据经父类继承，见 §5.4） | image:String | — | Any（OCR结果） |
| get_private_file_url | 可定稿 | file_id:String | — | Object（私聊文件URL信息）：url:String（Optional） |
| get_group_files_by_folder | 可定稿（bundle 侧；与网关规则冲突，见 §4） | group_id:String、file_count:Union[Number, String]†(default `50`) | folder_id:String、folder:String | Object（群文件夹文件列表）：files:Array\<Unknown\>、folders:Array\<Unknown\> |

## 2. trans / move / rename 三名复核

任务给定的先验说法——三者是独立动作、trans 仅 `group_id+file_id`、
move 带 `current/target_parent_directory`、rename 带
`current_parent_directory+new_name`——**经抽取全部复核成立**，证据为
JSON 中三者的 `class_name`、`payload_schema.schema_var` 与
`payload_fields`：

| 动作 | 类（JSON `class_name`） | schema 变量 | 字段表（JSON `payload_fields`） |
|---|---|---|---|
| trans_group_file | e_e | JSe | group_id:String（必填）、file_id:String（必填）——仅此两字段 |
| move_group_file | XSe | VSe | group_id、file_id、current_parent_directory、target_parent_directory（四字段全必填） |
| rename_group_file | r_e | t_e | group_id、file_id、current_parent_directory、new_name（四字段全必填，无 target_parent_directory） |

三者差异点：move 与 rename 共享前三字段后分叉——move 的第四字段是
`target_parent_directory`（目标父目录），rename 的第四字段是
`new_name`（新文件名）且没有 target 字段；trans 比两者少两个字段。
三者 return 同形而描述不同：均为 Object `{ok:Boolean}`，描述分别为
「转发文件结果」「移动文件结果」「重命名文件结果」。

注意与登记表的张力：`companion/adapters/action_registry.py` 现将
move_group_file / rename_group_file 登记为 `alias_of="trans_group_file"`
（N0 定名结论），但 bundle 证据显示三者是**三个独立类、三个独立
schema、字段互不相同**，按别名合并会丢失 move/rename 的必填字段。
本工件只提供证据，登记表处置留 N5 主线决策（本工件未改登记表）。

## 3. test_download_stream 定性：测试专用（合成流）

证据在 JSON `test_download_stream_analysis`（handler 摘录 +
`mechanical_facts` 机械判定位），不依赖动作名望文生义：

- payload schema 仅一个字段：`error:Boolean`（Optional，default
  `!1`，description「是否触发测试错误」）；payloadExample 里的
  `url: "http://example.com/file"` **不在 schema 内**
  （`actions.test_download_stream.notes` 已自动标注此矛盾）。
- `_handle` 摘录原文：循环 10 次、每次间隔 100ms 经流通道 `send`
  合成文本 ``Index-> ${o + 1}``（data_type `data_chunk`），其后若
  `e.error` 为真则抛字面量错误 `This is a test error`，最后返回
  `data_complete` / `Stream transmission complete`。
- 机械判定位：`uses_url_param=false`、`calls_core_apis=false`、
  `calls_network_or_file_io=false`、`sends_synthetic_index_chunks=true`、
  `throws_test_error_literal=true`、`loop_iterations_literal_10=true`；
  handler 实际只访问参数字段 `error`。

结论：该动作不读 URL、不调用任何 NapCat 核心 API、不做真实下载，
只是产生合成流数据并可按入参触发测试错误——**测试专用动作**，
与 N0 登记表中「内部/测试名排除」一类口径相符（登记处置不在本工件
范围）。

## 4. 与 gateway `_PARAM_RULES` / 登记表的自检

对照 `companion/adapters/gateway.py` 的 `_PARAM_RULES`（本工件 40 个
动作中仅两条有规则条目）与 `companion/adapters/action_registry.py`：

| 动作 | 对照结果 |
|---|---|
| get_group_file_url（对照组） | **一致**。bundle：必填 group_id:String + file_id:String（JSON `payload_fields`）；`_PARAM_RULES` 为 `(("group_id", "file_id"), ())`。登记表标 schema=verified，本证据与之吻合。 |
| get_group_files_by_folder | **冲突（如实记录）**。bundle：folder_id 与 folder 均为 Optional，file_count 为 Union[Number, String]、非 Optional 但带 default `50`；`_PARAM_RULES` 却把 folder_id 列为必填、且未提 file_count。该动作在登记表标 schema=verified / implemented——网关规则比 bundle schema 更严（folder_id），且与 bundle 的字段可选性定义不一致。处置（改网关规则或维持更严口径）留 N5 主线，本工件不改 gateway。 |
| get_private_file_url | 无冲突：bundle 必填仅 file_id:String；`_PARAM_RULES` 无此动作条目。 |
| 其余 37 个动作 | `_PARAM_RULES` 均无条目，无可冲突项。 |

另：脚本两遍运行输出 `diff` 一致；`source.md5_matches_n0_snapshot=true`
（bundle 未变，证据基线与 N0 同一文件）。

## 5. caveats（不改变 payload 定稿结论，但落契约必须知道）

### 5.1 return 为 Any 的 28 个动作

delete_group_file、delete_group_folder、create_flash_task、
send_flash_msg、download_fileset、get_flash_file_list、
get_flash_file_url、get_fileset_info、send_online_file、
send_online_folder、get_online_file_msg、receive_online_file、
refuse_online_file、cancel_online_file、upload_file_stream、
download_file_stream、download_file_image_stream、
download_file_record_stream、test_download_stream、
upload_image_to_qun_album、upload_images_to_qun_album、
upload_video_to_qun_album、set_group_album_media_like、
cancel_group_album_media_like、del_group_album_media、
do_group_album_comment、get_group_album_media_list、ocr_image
——以上动作 bundle 的 returnSchema 本身即 `p.Any({description})`
（JSON `return_schema.type="Any"`，描述已逐条记录在
`return_description`），**bundle 内不存在字段级 return 定义可供抽取**；
这不是抽取缺失，而是 bundle 定义的上限。若主线需要这 28 个动作的
return 字段级契约，证据路径只能是官方文档或受控实测，不在本工件
（离线 bundle 证据）范围内。

### 5.2 payloadExample 与 schema 矛盾的动作（示例不采，以 schema + handler 为准）

- receive_online_file：示例含 `save_path`（schema 无此字段）且缺必填
  `element_id`；schema 字段与 `handler_field_accesses`
  （user_id、msg_id、element_id）完全一致，故示例作废。
- refuse_online_file：示例缺必填 `element_id`（其余两字段一致），
  同上以 schema 为准。
- test_download_stream：示例 `url` 不在 schema 内（见 §3）。
- create_flash_task：`validitySeconds` 的 description 文案称「默认
  1209600」，但该字段的 TypeBox 选项里**没有 default 属性**
  （JSON 中 `has_default=false`）——文案与定义不一致，落契约时按
  「无 schema 默认值」处理。
- 多个动作的示例以引用形式给出（如 `et.UploadPrivateFile.payload`、
  `Qo.GetGroupFileUrl.payload`、`In.OCRImage.payload`），JSON
  `payload_example.kind="reference"` 只记录引用原文、不展开——示例
  本就不是字段证据，定稿不依赖它。

### 5.3 schema 未表达的约束（字段表照录，约束靠 handler/check 旁证）

- download_file 四字段全可选、download_file_stream /
  download_file_image_stream / download_file_record_stream 全部字段
  全可选：schema 层面没有任何必填字段；「url 与 base64 二选一」
  「file 与 file_id 二选一」这类约束在 TypeBox 定义中不存在，
  落契约时不得把它们写成 schema 必填。
- create_group_file_folder 的 folder_name 与 name 均为可选同义字段；
  delete_group_folder、get_group_files_by_folder 的 folder_id 与
  folder 均为可选同义字段——schema 不保证调用方至少传一个。
- send_flash_msg 的 user_id 与 group_id 均为可选：目标二选一未在
  schema 表达。
- do_group_album_comment 例外：lloc 与 batch_id 在 schema 中均为
  可选，但类内 `check()` 明确强制「lloc 或 batch_id 至少提供一个」
  （JSON `actions.do_group_album_comment.check_excerpt` 原文），
  这是 bundle 内成文的类级约束，落契约时应写入。

### 5.4 继承证据

ocr_image 的动作类 `Dbe` 本身不带 schema，其 payloadSchema
（image:String 必填）、returnSchema 与 `_handle` 全部继承自父类
`IL`（JSON `inherited_from=["IL"]`、`handler_inherited_from="IL"`、
`handler_field_accesses=["image"]`）。定稿成立，但证据链比自带
schema 的动作多一跳，主线引用时应注明继承来源。另
set_group_album_media_like 与 cancel_group_album_media_like 共用
同一对 schema 变量 NL（payload）/ PL（return），两者的字段表
因此逐字相同，这是 bundle 的定义方式、不是抽取串扰（两动作的
`payload_schema.schema_var` 均记录为 NL）。

## 6. 消息段证据：flashtransfer 与 online_file / onlinefile

证据在 JSON `segment_evidence`：

- **flashtransfer：可定稿。** 段 schema `$id="OB11MessageFlashTransfer"`
  的 `data` 只有一个字段：`fileSetId:String`（必填，description
  「文件集ID」）。raw→ob11 产出点：`markdownElement` 在
  `mdExtInfo.flashTransferInfo.filesetId` 存在时产出
  `{type: ze.flashtransfer, data: {fileSetId}}`（JSON
  `segment_evidence.raw_to_ob11_converters` 摘录）。
- **online_file：该字面量在 bundle 中无段证据；实际段类型是
  onlinefile（无下划线）。** `segment_evidence.online_file_literal_found
  =false`（无 `p.Literal("online_file")`、无 `t.online_file` 枚举项；
  `online_file` 只出现在动作名/事件名中）。按实际段类型 onlinefile
  抽取则**可定稿**：`$id="OB11MessageOnlineFile"`，`data` 五字段全
  必填——msgId:String、elementId:String、fileName:String、
  fileSize:String、isDir:Boolean。raw→ob11 产出点：`fileElement` 在
  elementType 为 23 或 30 时产出该段（isDir 由 elementType===30
  判定，摘录同上）。
- **两段均为「只入不出」**：ob11→raw 转换器中两者都是空实现
  `async () => {}`，其上方有原文注释「不需要支持发送」（JSON
  `segment_evidence.ob11_to_raw_converters` 摘录）——经 send_msg
  发送这两类段在 bundle 层面无实现，落契约时只能按入站段处理。

## 7. 复跑方式

```bash
export PATH="$HOME/.local/bin:$PATH"
uv run python tests/scripts_n5/extract_bundle_schemas.py \
  --bundle ~/agent-workspace/projects/QQBot-deploy/napcat-v4/root/opt/QQ/resources/app/napcat/napcat.mjs \
  --out tests/fixtures/napcat/bundle_action_schemas.json \
  --snapshot tests/fixtures/napcat/bundle_registry_snapshot.json
```

本工件仅新增 `tests/scripts_n5/`、上述 JSON 与本文档，未改动
`companion/`、既有测试与登记表。
