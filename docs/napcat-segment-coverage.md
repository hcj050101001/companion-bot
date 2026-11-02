# NapCat 消息段覆盖矩阵（N0 交付物）

来源：全接口接入方案 §2.2 × 调研报告段清单 × 框架现行段模型。
机器可读版：`tests/fixtures/napcat/segment_coverage.json`（与本表同源生成）。
出站构造口径：简单段序列继续 CQ 字符串 + 令牌文法校验；forward/node 等嵌套结构段走 segment 数组报文。

| 段 | 方向 | 现行建模 | 接入处置 | 验证状态 | 约束/备注 |
|---|---|---|---|---|---|
| text | 双向 | TextSegment | 一等段 | 已证实 | 不变 |
| at | 双向 | AtSegment | 一等段 | 已证实 | 补 @全体（qq=all）语义 |
| reply | 双向 | Raw | 一等段 | 已证实 | 提为一等段（引用回复是群聊基本形态） |
| face | 双向 | Raw | 一等段 | 已证实 | 提为一等段 |
| mface | 双向 | Raw | 一等段 | 已证实 | 商城表情，含 super-emoji 字段 |
| image | 双向 | ImageSegment | 一等段 | 已证实 | 补 summary/sub_type 字段口径 |
| record | 双向 | AudioSegment | 一等段 | 已证实 | 不变 |
| video | 双向 | Raw | 一等段 | 已证实 | 提为一等段（含 thumb） |
| file | 双向 | Raw | 一等段 | 已证实 | 提为一等段（与文件域接口配套） |
| poke | 双向 | Raw | 一等段 | 已证实 | 提为一等段（互动形态） |
| dice | 双向 | Raw | 一等段 | 已证实 | 提为一等段 |
| rps | 双向 | Raw | 一等段 | 已证实 | 提为一等段 |
| forward | 出站为主 | Raw | 结构化段 | 已证实 | 提为结构化段：节点树有嵌套结构，出站走 segment 数组报文，不用 CQ 拼接 |
| node | 出站为主 | Raw | 结构化段 | 已证实 | 同 forward |
| json | 双向 | Raw | Raw + 字段口径 | 已证实 | 保持 Raw 级别但登记字段口径（Ark 卡走 json）；出站须过递归复核 |
| xml | 双向 | Raw | Raw + 字段口径 | 已证实 | 保持 Raw 级别但登记字段口径 |
| music | 双向 | Raw | 已知段（Raw 构造） | 已证实 | 登记为已知段，构造经 Raw + 令牌校验路径 |
| contact | 双向 | Raw | 已知段（Raw 构造） | 已证实 | 同 music |
| location | 双向 | Raw | 已知段（Raw 构造） | 已证实 | 同 music |
| markdown | 双向 | Raw | 已知段（Raw 构造） | 已证实 | 同 music |
| miniapp | 双向 | Raw | 已知段（Raw 构造） | 已证实 | 同 music |
| flashtransfer | 入站 | Raw | 只收不发（receive-only） | bundle 证实（N5 定稿） | data 仅 fileSetId 必填；入站 RawSegment + 关键字段校验，出站序列化明确拒绝（平台 ob11→raw 转换器为空实现） |
| onlinefile | 入站 | Raw | 只收不发（receive-only） | bundle 证实（N5 定稿） | **正名**：平台字面量为 onlinefile（无下划线），online_file 无段证据、仅见于动作名/事件名；data 五字段全必填（msgId/elementId/fileName/fileSize/isDir）；可收不可发，同上 |

## 自审记录（N0）

- 段集与方案 §2.2 全量一致：现行已建模 5 类（text/at/image/record + Raw 透传），本表登记 23 类。
- **待验 2 类**：`flashtransfer` 与在线文件段——对应动作族已证实在册，但段形态 schema 无双源证据，随 E2 族在 N5 验证后补入，不在本阶段臆测字段。
- receive-only 考量：本机钉版下无仅可收不可发的段；`forward`/`node` 以出站构造为主，入站合并转发经 get_forward_msg 展开（内容形态在动作登记侧）。
- 组合约束：forward/node 的节点树可嵌套 node 与任意段，出站复核须递归遍历（方案 §4 C5）；json 卡片的可见文本同样过复核。

## N2 落地附记（2026-10-31，分支 ai/qqbot-napcat-n2）

上表为 N0 时点口径，逐行保留不动；N2 的实际落地与之的对应关系：

- 「一等段」行（reply/face/mface/video/file/poke/dice/rps）已建模为
  同名 Segment 类（`companion/adapters/base.py`），入站解析与出站
  构造双向落地；forward/node 为结构化段（NodeSegment 带 content
  段元组），出站走 segment 数组报文。
- 「已知段（Raw 构造）」行（json/xml/music/contact/location/
  markdown/miniapp）维持 RawSegment 承载，登记于
  `KNOWN_RAW_SEGMENT_TYPES`；标量载荷走 CQ 令牌校验路径、嵌套载荷
  走数组报文。at 已补 @全体语义（qq=all 与普通 qq 同路序列化）。
- image 的 summary/sub_type 等字段经 data 透传保留（CQ 全量序列化
  与数组报文均不丢字段），无单独口径变更。
- 待验 2 类（flashtransfer/online_file）仍未建模，归 N5。
- 一致性由 `tests/test_segments_n2.py` 的矩阵×实现面断言守护
  （SUPPORTED_SEGMENT_TYPES 与适配器能力声明同源）。

## N5 落地附记（2026-10-31，分支 ai/qqbot-napcat-n5）

上表 N0 两行（flashtransfer / online_file 待验）已按 bundle 段证据
定稿并在表中改判，下为口径说明（证据全文：
`docs/napcat-schema-evidence-n5.md` §6，机器证据
`tests/fixtures/napcat/bundle_action_schemas.json` 的
`segment_evidence`）：

- **flashtransfer**：段 schema `$id="OB11MessageFlashTransfer"`，
  data 仅 `fileSetId:String` 必填。入站解析产出 RawSegment 并校验
  该字段（缺失/非字符串即整条消息判 malformed）；登记于
  `KNOWN_RAW_SEGMENT_TYPES`（含于 `SUPPORTED_SEGMENT_TYPES`，
  「可收」面）。
- **onlinefile（正名）**：原登记名 online_file 在 bundle 中无段
  证据（无 `p.Literal("online_file")`、无对应枚举项，只出现在
  动作名/事件名中）；实际段类型是 **onlinefile（无下划线）**，
  `$id="OB11MessageOnlineFile"`，data 五字段全必填——msgId /
  elementId / fileName / fileSize（均 String）、isDir（Boolean）。
  入站校验同上。
- **只收不发**：两段的 ob11→raw 转换器在 bundle 中均为空实现
  `async () => {}`，上方原文注释「不需要支持发送」——经消息段
  发送这两类在平台层面无实现。出站序列化（CQ 与 segment 数组
  两条路径）遇此两类一律返回 None（与既有不可序列化段同一
  拒绝口径，发送前 rejected，绝不静默丢段）；含此两段的
  NodeSegment 内容同样整体拒绝。
- 一致性由 `tests/test_segments_n2.py` 的矩阵×实现面断言（更新后
  口径）与 `tests/test_files_media_n5.py` 的入站解析/出站拒绝
  断言守护。
