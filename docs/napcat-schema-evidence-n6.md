# NapCat schema 字段级证据（N6）

阶段：NapCat 全接口接入 **N6（敏感面与收口）** 的证据工件。基线 bundle：
本机 NapCat **4.18.41** 的 `napcat.mjs`（MD5
`7fdb4903c8867e7db0b3345ad226c805`，3,134,858 字节，与 N0 快照
`tests/fixtures/napcat/bundle_registry_snapshot.json` 的 `bundle_md5`
一致，脚本自检 `md5_matches_n0_snapshot=true`）。

工件：

- 抽取脚本 `tests/scripts_n6/extract_bundle_schemas.py`（与 N5 抽取引擎
  同一方法：注册表 `const re = {...}` 反查动作键名 → `actionName` 定位
  动作类 → 解析类内/父类链 `payloadSchema` 的 TypeBox 定义原文逐字段
  抽取，并以 `_handle` 字段访问作旁证；只读解析 bundle，不联网、不调用
  真实 NapCat；输出无时间戳，连跑两遍 `diff` 逐字节一致）；
- 证据 JSON `tests/fixtures/napcat/bundle_action_schemas_n6.json`
  （`summary.requested=14, found=12, not_found=[.get_word_slices,
  get_group_todo]`）。

范围：N6 终审清单的 14 个动作——L3 敏感族 8 个（schema 补核 + 解锁
契约依据）、`get_share_link`（N5 遗留补抽）、`get_unidirectional_friend_list`
（在册但 unverified 的在册项）、ops_only 两条（`bot_exit` / `set_restart`，
处置终审的事实面）、以及两条查无类者（`.get_word_slices` /
`get_group_todo`）。

**结论口径**与 N5 相同：「升 verified」= payload schema 字段级证据完整
（字段名/类型/Optional/default/description 均来自 bundle 内 TypeBox
定义原文）。return 侧只记抽取事实：本批多条 return 为凭证类数据，
**字段级证据的存在不构成任何实调许可**——L3 族在 N0 起即从未实调，
本轮同样只做静态抽取。

## 1. 逐动作结论表

字段标注：`名:类型`；† = 非 Optional 但带 default（bundle 原值照录）；
return 列 `Any（描述）` 表示 bundle 的 returnSchema 本身为 `p.Any`，
无字段级定义。

| 动作 | payload | return | 处置结论 |
|---|---|---|---|
| get_cookies | `domain:String`（必填） | Object{cookies:String, bkn:String} | 原 verified 不变；网关 `_PARAM_RULES` 钉 domain 必填 |
| get_csrf_token | （空） | Object{token:Number} | 原 verified 不变 |
| get_credentials | `domain:String`（必填） | Object{cookies:String, token:Number} | 原 verified 不变；网关钉 domain 必填 |
| get_clientkey | （空） | Object{clientkey:String?} | **unverified → verified** |
| nc_get_rkey | （空） | Array（item Any，「Rkey列表」） | 原 verified 不变 |
| get_rkey | （空） | Array of Object{type, rkey, created_at, ttl} | **unverified → verified** |
| get_rkey_server | （空） | Object{private_rkey?, group_rkey?, expired_time?, name} | **unverified → verified** |
| send_packet | `cmd:String`（必填）、`data:String`（必填，十六进制）、`rsp:Union[String,Boolean]†`（default `!0`＝true） | Union[String, Undefined] | **unverified → verified**；门面据此类型化（cmd/data/rsp），网关钉 cmd+data 必填 |
| get_share_link | `fileset_id:String`（必填） | Any（「分享链接」） | **unverified → verified**（payload 定稿；return 为 p.Any，入 §3 口径） |
| get_unidirectional_friend_list | （空） | Array of Object{uin, uid, nick_name, age, source} | **unverified → verified** |
| bot_exit | （空） | Object{}（空对象） | **unverified → verified**（仅记抽取事实；处置维持 ops_only，见收口文档） |
| set_restart | （空） | Null | **unverified → verified**（同上） |
| .get_word_slices | —（无动作类） | — | found=false，见 §2 |
| get_group_todo | —（不在注册表） | — | found=false，见 §2 |

合计：schema verified 130 → **138**（本表 8 条升档）。

## 2. 两条 found=false 的证据

- **`.get_word_slices`**：注册表对象有键 `GoCQHTTP_GetWordSlices`
  （值为 `".get_word_slices"`），但全 bundle 无
  `actionName = re.GoCQHTTP_GetWordSlices` 的动作类。补充全文核查：
  标识符 `GoCQHTTP_GetWordSlices` 在整个 bundle 中**仅出现 1 次**
  （即注册表对象那一行，字节偏移 1074363）；对照同族的
  `.handle_quick_operation` 则有真实动作类绑定。结论：该键是注册表
  内的占位/残留键，4.18.41 **无 handler**。N6 终审据此把登记表处置
  自 `registered` 改判 **`excluded_internal`**（与 `unknown` 占位键
  同形态），门面同步下线。N0 当年「以本机为准」的在册登记是仅凭
  注册表对象得出的，本次属证据升级后的纠正、非口径漂移；恢复路径
  写在登记表条目 notes（NapCat 升级重跑抽取出现动作类，或值守
  WebUI Debug 实调证实可达）。
- **`get_group_todo`**：动作名根本不在 bundle 注册表 `const re` 中
  （`registry_key=null`），与 N0 第 2 级实调回「不支持的Api
  get_group_todo」（与负对照同形）互相印证。处置维持
  **pending_verification**：待办族写三名在册而查询名无登记，不排除
  未来版本补齐或别名存在；验证路径 = NapCat 升级复核 + 值守 WebUI
  Debug 全表浏览（见收口文档挂起清单）。

## 3. p.Any return 口径（与收口文档 §4 同步）

`get_share_link` 的 return 是第 29 个 `p.Any`（N5 §5.1 已登记 28 个）。
N6 终审裁决：**维持三态 + 平台 data 透传，不为 p.Any 动作编造字段级
return 契约**。理由：bundle 本身未定义字段，任何字段表都只能来自
实调采样，而采样即真实调用——对 L0 读动作虽可行，但为 29 个低频
动作逐个采样建约的收益低于契约失真风险；网关的返回面契约（三态 +
`data` 原样透传 + 审计不记返回值）已覆盖调用方需求。若未来某动作
出现产品级字段依赖，再按单动作立项采样定约。
