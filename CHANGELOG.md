# Changelog

本项目的所有显著变更都记录在本文件。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循语义化版本（Semantic Versioning），以 git tag `vX.Y.Z` 发布：

- **MAJOR（X）**：不兼容的契约/接口变更；
- **MINOR（Y）**：向后兼容的功能新增；
- **PATCH（Z）**：向后兼容的缺陷修复。

发布约定（方案 D4）：

- 框架迁移期（P0 起、框架化未完成前）只打 `v0.9.x` 系列 tag；
- **`v1.0.0` 只在框架迁移完成且真机冒烟留痕后才允许打**——隔离/夹具
  测试通过不构成 1.0 依据，必须有真实 QQ 私聊端到端冒烟记录可查；
- 每个 tag 对应本文件一个已发布段（段名、版本号与日期与 tag 一致），
  `Unreleased` 段的条目在打 tag 时整体移入该版本段。

## [Unreleased]

## [0.10.0] - 2026-11-02

> 整理说明（2026-11-01，开源化阶段三）：本段积压条目已按
> Keep a Changelog 分类重新归位——NapCat 全接口接入 N0–N6 属
> 功能新增、自 Fixed 段移入 Added；两段同名 Fixed 已合并。
> 条目正文与事实口径未改。首个公开版本已按决策 D7 以
> `v0.10.0` 切段发布（2026-11-02，github.com/hcj050101001/
> companion-bot）。

> 前移记录（2026-11-02，开源公开发布前移轮）：断线补回、
> action-verify 改判与危机告警启用三段部署全部落地，现网
> tip 自 `d165439` 推进至定版 `481ba26`，准备线合并前移
> （生产侧 13 文件与准备线 47 文件零交集、零冲突；增量 =
> 断线补回模块组与历史拉取适配、注册表两条改判、配置示例
> 补回段与两篇文档）。本轮实测刷新：全套 3025 passed /
> 4 skipped（现网部署口径；准备线同树实测 3022 passed /
> 7 skipped，差额 3 项为布局相关的部署资产测试在非生产
> 布局下按设计跳过，与前几轮同一现象）、总覆盖率 90.3%
> （本机 cov 实测 TOTAL 90%，门槛 85）、测试函数 1618 个
> （同一计数法，旧树 1543）、注册表全表 191 条 /
> canonical 167 个（两条改判为状态变更，计数口径不变）。
> 另按负责人 2026-11-02 裁决①为 `.gitleaks.toml` 补 uv.lock
> 值级豁免（ruff wheel `size` 字节数 14 个确切值，附注记）。

> 前移记录（2026-11-02，开源化增量刷新轮）：识图记忆部署
> 上线，现网 tip 自 `64f5375` 推进至 `d165439`，准备线合并
> 前移（生产侧 7 文件与准备线文件集零交集、零冲突）。本轮
> 实测刷新：全套 2950 passed / 4 skipped（现网部署口径；
> 准备线同树首跑 2947 passed / 7 skipped，差额 3 项为依赖
> 仓库外运行时资产的部署资产测试——runtime unit 两项与
> /etc drop-in 一项，随自动恢复巡检的恢复状态浮动、与代码
> 无关，巡检恢复后同树复跑即 2950 passed / 4 skipped）、
> 总覆盖率 90.0%（89.81%，门槛 85）、注册表全表 191 条 /
> canonical 167 个（口径不变）。

> 前移记录（2026-11-02，开源化最终前移轮）：准备线基线自现网
> `5892d34` 合并前移至现网最终 tip `64f5375`（封版四步全部
> 落地：seg3 集成线、倒计时本体 + 增强均已上线；合并双方文件
> 集零交集、零冲突）。本轮实测刷新：全套 2883 passed /
> 4 skipped（现网部署口径；准备线同树实测 2880 passed /
> 7 skipped，差额 3 项为布局相关的部署资产测试在非生产布局下
> 按设计跳过）、总覆盖率 90.0%（89.98%，门槛 85）、注册表
> 全表 191 条 / canonical 167 个（口径不变）。

> 前移记录（2026-11-01，开源化前移轮）：准备线基线自 `e1470f1`
> 变基至现网 `5892d34`（12 个准备线提交零冲突重放；生产代码以
> 现网为准、准备线文档/打包/CI 保留并适配）。本轮实测刷新：
> 全套 1796 passed / 7 skipped（跳过中 3 项为布局相关的部署资产
> 测试按设计跳过、4 项为生产装配门控跳过）、总覆盖率 88.8%
> （门槛 85）、注册表全表 191 条 / canonical 167 个
> （L0 61 / L1 62 / L2 36 / L3 8）。

### Added

- **断线重连补回（2026-11-02 上线）**：NapCat 断线期间错过的
  消息不再静默丢弃——连接健康由坏转好且稳定 ≥120s 后触发
  补回扫描（窗口硬上限 24h），经已接入的历史动作按会话捞回，
  以 message_id 去重账本防重复处理；每会话取最新 10 条合成
  一个补处理回合走原 11-Stage 管线，单轮 ≤20 个会话。补回
  回复带前缀「（刚信号不好，才看到你之前的消息）」；断线期
  危机消息仍走现行危机协议；群聊仅 @ 触发口径不变、还原
  不了宁漏勿扰。历史 message_id 保真已在部署时以真实断线
  窗口消息验证，开关已开启。

- **识图记忆持久化（2026-11-02 上线）**：通过危机筛查的识图
  描述在回复投递成功后持久写入对话记忆（无 TTL），之后用纯
  文字追问也能接续刚才图片的内容；危机图片描述永不落存，
  「忘掉」时视觉观察一并删除。落存文本经结构化隔离与按等价
  类净化（外部素材框架包裹、定界符与伪造角色行打断、落盘形
  态 2000 字上限），并建内容同一性索引支持同图跨上传的补偿
  删除。已随现网部署上线；真机复验（发图后纯文字追问接续、
  忘掉后不再提及）待负责人晨间执行。

- **纪念日倒计时工具与对话管理（2026-11-02 上线）**：新增只读
  工具 `date_countdown`：配置静态清单（两种条目形态，混写在
  加载期 fail-fast 拒绝）之外，主人可在私聊对话中增删查纪念
  日——主人存入的条目持久落盘、仅主人可见可管理（他人按名查
  询与条目不存在同口径，连存在性都不泄露），整表清单仅主人
  可枚举；对话中可自然问「离 XX 还有几天」。生产上线时清单
  为空表。

- **图片内容危机筛查（F7，2026-11-01 上线）**：识图描述在进入
  回复生成前先过危机筛查（与随图文字同一口径），跟随
  `media.vision` 开关；命中即走既有危机处置协议与固定话术，
  危机图片描述不落存任何记忆；描述原文不写入日志。

- **私聊图片理解接入（deepseek-flash 视觉）**：图片段经 ResourceResolver
  口径取图（出站目标 SSRF 校验、手动逐跳重定向复检）、魔数嗅探、
  base64 data URL 随 user 消息 content 块进入对话；视觉准备在
  SafetyJudge 与入站限流之后的 Respond 执行；纯图缺省语境、图文
  混合、单轮上限/单图上限；失败口径：纯图回退原固定文案（经投递
  代次围栏）、图文回落既有文本路由；新增配置 `media.vision`
  （默认开）；属有意行为变更（用户 2026-10-31 指令），原「纯媒体
  固定回复」口径仅在视觉不可用/失败/无可取引用时保留。

- **群回复开启与好友申请全自动同意（2026-10-31 用户拍板）**：群回复
  产品接线——`tools.group_chat.reply_enabled` 开时 EventProcessor
  装配默认生产 responder（仅 @ 触发、空 history、基础人设、不调
  工具、不带私聊记忆，输出段过滤与危机不入群链口径不变），N4
  「responder 恒 None」装配断言随之改判并注明依据。新增配置段
  `requests`：`friend` 可取 `approve_all`（全自动同意所有好友
  申请），`group` 在配置层不开放 `approve_all`（群申请保持
  manual/allowlist）；策略存在自动同意时组合根装配
  GatewayRequestChannel（system 主体、account 级授权、配置层
  常开确认），经既有 exactly-once 状态机与 ActionGateway 执行
  `set_friend_add_request`；自动模式已配置但无网关时启动期
  fail-fast。全 manual 时行为与接线前逐字一致。

- **NapCat 全接口接入 N0（验证与登记固化）**：本机 NapCat 4.18.41
  bundle 动作注册表对象快照固化为夹具（未注释 182 / 注释 8，
  `tests/fixtures/napcat/`，抽取脚本 `tests/scripts_n0/`）；调研报告
  命名集 × bundle 注册表 × 方案登记表三方对账全绿（报告支持类 136
  全部在册、文档冲突 3 以本机为准、注册表独有候选 43 = 补登记 39 +
  内部/测试名排除 4）；登记表落成代码内注册表骨架
  `companion/adapters/action_registry.py`（191 条 = 在册 182 + 挂起 9，
  canonical 功能动作 167，按方案 §1.1 五元组登记、已接入计数 0）；
  §7.3 第 2 级只读探针 15 次（仅 `get_*` 空参，13 个在册动作回 400
  schema 校验错、get_group_todo 与负对照同形回「不支持的Api」）；
  定名 10 项落定（trans_group_file、get_group_ignored_notifies、
  refuse_online_file、get_online_file_msg、del_group_album_media、
  do_group_album_comment、set_group_kick_members 等）；事件（32 行）
  与消息段（23 行）覆盖矩阵成文（`docs/napcat-*-coverage.md`），
  lucky_king/honor 标单源存疑；验证记录见
  `docs/napcat-verification-n0.md`。本阶段零运行时行为变更，现网未动。

- **NapCat 全接口接入 N2（消息形态全量，含 C8 投递重构）**：段全集
  建模（reply/face/mface/video/file/poke/dice/rps 提为一等段、
  forward/node 为结构化段、json/xml/music/contact/location/markdown/
  miniapp 为已知 Raw 段；flashtransfer/online_file 仍待验归 N5）；
  出站序列化双形态（CQ 字符串优先、嵌套结构走 segment 数组，编码
  选择是段结构的函数）；DeliveryService 引入 SendTarget 结构化寻址
  与 `send_to` 段通道，SendQuota 落地两级额度（群 per-group 子桶 +
  账号群发子预算，与私聊共享同一账号总池、合计不突破总上限，
  critical 预留池不可借用且不可由调用方自声明）；出站段级规划
  （拆分只作用于纯文本、富段原子保序）与递归复核（forward/node/
  json/文件名可见文本与纯文本同一套规则、媒体段默认关须显式
  开启）；send_group_msg/send_msg/forward 系/AI 语音/poke/emoji_like/
  已读标记等 owner=N2 的 31 个动作经门面接通（登记表已接入计数
  62 → 93）。群发送运行时默认硬关（Gateway policy 默认 disabled +
  DeliveryService 群开关默认关 + 无生产调用方），仅 Fake 验收；
  私聊全路径行为逐项等价（既有 963 项测试不改口径全绿，N1 两处
  「群发送门面一律拒」断言按路线图改判为分层门控断言并注明）。
  现网未动、未部署。

- **NapCat 全接口接入 N3（入站事件面全量，2026-10-31）**：入站判别
  联合四型 + classify 落地，私聊链行为与日志零变化、11 Stage 顺序
  契约逐字不变；群消息入群聊子链只观察登记
  （`event_type="group_message"`，危机关键词仅记
  `reason_code="crisis_keyword"` 不进私聊危机链，匿名/系统型记
  `registered_only`，队列满记 `shed`）；notice 全子类登记
  （`event_type="notice"`，状态类同键合并计 coalesced），bot 被踢/
  被禁言/离线三类运维升级在登记后追加 `ops_alert`；request 入事件
  子链并经落库处置状态机（唯一键 `(self_id, request_type, sub_type,
  flag)`、默认人工、ambiguous 不重驱，Fake 通道端到端验证只处置
  一次）；meta 登记为 `meta_event`、message_sent 方向契约落地
  （只登记、不建 claim）；event/group/request 三道有界队列与入队前
  合并使事件风暴不挤占私聊/危机通道；heartbeat 对账规则落地
  （探针为准、事件为辅）。事件 golden 夹具改判 7 条并新增 27 条
  （每事件子类至少一条），覆盖矩阵每行标注 implemented /
  behavior_verified（lucky_king/honor 处理已落地、平台存在性仍
  单源存疑），行为测试新增 `tests/test_inbound_events_n3.py`。
  现网未动、未部署。Codex Review 6 条（P1×3、P2×3）已修复并补
  回归：群 claim 改走独立 group 辅助分区、与私聊 ordinary 分区
  隔离（partitions/counts 出口不变）；落库 pending 的 request 由
  事件消费者启动时有界清扫续驱；事件道单项异常只记日志并继续
  循环、不再终止整道；GatewayRequestChannel 的 principal/确认
  改为接线层显式注入（白名单外主体构造即拒）；classify 对结构
  畸形（字段缺失/非 str/非 token 形）改判 malformed、合法 token
  枚举外仍 unknown_event_type；NoticeRateBudget 限流键补齐为
  (self_id, 群, 事件族) 三维（notify 按 sub_type 分族）。

- **NapCat 全接口接入 N4（群场景与 L2 管理，2026-10-31）**：L2
  授权链在 Gateway 的 canonical 路径落地（方案 §5）——逐资源授权
  上下文 AuthorizationContext 必备且目标（group/user/account 由
  参数派生）须在范围内，无授权/越界零派发被拒；不可逆档
  （set_group_leave、delete_friend）须持绑定确认令牌（动作+参数
  摘要+目标+有效期+单次 nonce，revoke 后全失效），confirmed 布尔
  不足放行；群管理动作真执行前经 role_resolver 复核机器人群内
  角色（生产实现经网关自身 L0 查询 + TTL 缓存，缺失/不足 fail
  closed，设管理员与解散须群主）；L2 canonical 路径默认
  dry_run=true、须 policy 显式关。owner=N4 的 39 个动作（L2 群
  管理/精华/公告/群设置族/请求处置/删好友/账号资料/clean_cache
  等 33 + L1 群签到/群待办/好友备注/send_qzone_msg 6）经门面接
  通，登记表已接入计数 93 → 132；friend_management/qzone 两条
  legacy policy 路径沿用先例门控、不走 canonical 授权链（口径
  冻结）。qzone 迁移：legacy 合成 spec publish_message 下线，
  qzone_post 改调 send_qzone_msg（policy_key=qzone 不变）。
  request 处置：RequestPolicy 增 allowlist 模式（只自动同意名单
  内、永不自动拒绝），默认仍 manual；GatewayRequestChannel 增
  authorization 显式注入（缺失构造即拒），生产接线保持
  channel=None。群回复通路：产品开关 tools.group_chat.
  reply_enabled（新增配置项，默认关、与能力开关分离）开时群聊
  子链在观察后追加 GroupAdmission/GroupRespond 两级——仅 @ 机器
  人触发、双维主体分级（人 × 群内角色）、responder 只拿事件与
  分级（无私聊记忆/状态）、产出剥 ReplySegment（默认不引用）、
  发送只经 DeliveryService 群通道（群门/媒体门/递归复核/两级
  额度即安全边界）；默认关时群链与 N3 逐项一致、零回复，生产
  无 responder 注入。行为测试新增 tests/test_group_domain_n4.py
  （56 项），N3 通道夹具与 N1 spec 断言各按 N4 口径改判并注明。
  现网未动、未部署。

- **NapCat 全接口接入 N5（文件与媒体域，2026-10-31）**：文件域
  核心契约落地（ResourceResolver 完整版、FileTaskManager 异步
  任务账与 stream_upload、ActionCircuitBreaker）；bundle payload
  schema 字段级证据定稿 40 个动作（证据文档与 JSON 工件入仓），
  登记表 28 条 schema 升 verified（102 → 130）。文件/媒体域 28 个
  canonical 接通（E 写 9 + E2 闪传/在线/流式写 12 + F 群相册写 7，
  新增 AlbumActions 门面），登记表已接入计数 132 → 160；L2 删除
  三动作（删群文件/删群文件夹/删相册媒体）入不可逆令牌档与群
  管理员角色档。定案纠正：trans/move/rename 经 schema 证据改判
  三个独立 canonical（别名 7 → 5、canonical 167 → 169）；
  test_download_stream 定性测试专用合成流、不接门面、终审归 N6。
  网关新增强化：F 域相册熔断（默认 threshold=3/cooldown=300s、
  门禁拒绝不计失败）、文件域落盘审计资源摘要（digest + 声明
  大小，原文不落日志）、ResourceResolver 可选接线（未注入行为
  不变）；flashtransfer/onlinefile（正名，无下划线）两段定稿为
  receive-only（入站 RawSegment + 关键字段校验、出站序列化明确
  拒绝）。OCR 经网关三态与资源边界归入本阶段契约。集成测试
  tests/test_files_media_n5.py（130 项）+ 契约单元
  tests/test_file_contracts_n5.py（54 项）；阶段记录见
  docs/napcat-files-media-n5.md，N0 待验表归 N5 项已在
  docs/napcat-verification-n0.md 末尾追记销账。现网未动、未部署。

- **NapCat 全接口接入 N6（敏感面与收口，2026-10-31，N 系列完结）**：
  L3 受控解锁落地——默认硬禁不变，解锁只经运维配置
  `actions.l3_unlock` 逐动作名单 + 网关构造期快照（改名单须重启、
  invoke 无解锁参数），解锁后仍须 ops_cli 主体 + account 授权
  上下文且全量审计、返回值永不入日志（tests/test_l3_unlock_n6.py
  21 项钉死，含配置变更不影响已运行实例的快照断言）；L3 八条
  schema 以 bundle 动作类证据补核，连同 get_share_link、
  get_unidirectional_friend_list 与 ops_only 两条共 8 条升
  verified（130 → 138，证据 docs/napcat-schema-evidence-n6.md）。
  登记表终审：test_download_stream 与 .get_word_slices 经证据升级
  改判 excluded_internal（后者门面同步下线、canonical L0 62 → 61），
  registered 172 / canonical 167，**已接入 167 = 全部 canonical
  在册条目**；pending 两条维持挂起并写明路径，ops_only 两条维持
  原处置。方案 §3.4 的 action_levels 落地（AdapterCapabilities
  新增字段，两适配器同源派生）。收口文档
  docs/napcat-integration-closeout.md 汇总终局处置、开关一览与
  切现网关系（切现网仍为用户单独决策）。全套 1445 项通过
  （Review 修复后 1447，见下 Fixed 条目），现网未动、未部署。

- **开源化准备 · 阶段三质量门（公开线暂存，未发布）**：pyproject
  元数据对齐（版本演练占位 0.10.0、description 按现行定位重写、
  license 与 classifiers 补齐）；coverage 真实接线（pytest-cov
  入 dev 依赖，实测总覆盖率 89%，`fail_under` 门槛设 85）；CI
  增强（Python 3.12/3.13 矩阵、uv 锁版、pytest 口径与
  scripts/test.sh 对齐 `--timeout=10`、coverage 工件、Docker
  构建验证 job、gitleaks 独立 job）；社区文件补齐（SECURITY.md、
  CODE_OF_CONDUCT.md、Issue/PR 模板）；CONTRIBUTING 重写、
  AGENTS.md 剥离内部约定；examples 新增 Fake 适配器端到端
  demo；本文件 Unreleased 积压完成结构化整理（N 系列条目归
  Added、双 Fixed 段合并）。

### Fixed

- **NapCat 动作注册表验证改判（action-verify，2026-11-02
  上线）**：两条长期挂 pending 的动作条目经真机验证后改判——
  一条为 not_supported、一条为 nonexistent（验证过程见
  `docs/napcat-action-verify-2026-11-01.md`）；已接入计数
  不变（canonical 167）。

- **出站内容校验收口（历史补审第三段集成线，2026-11-02 封版
  上线）**：网关与 services 双层审查统一到同一套提取实现——
  提取顺序与序列化顺序逐字对齐，语义键（决定取值的精确键）
  与覆盖键（决定送审范围的归一键）分离；字段序拆分短语、键名
  变体顶替两类绕过按等价类关闭。未清项按封版口径记
  `docs/accepted-residuals.md` §7 在案接受。

- **网关出站校验硬化（历史补审第二段修复线，2026-11-01 封版
  上线）**：出站资源引用校验收口为路径语义门（相对引用按部署
  根语义判定），CQ 与 dict 形态的资源参数提取补强；未清项按
  封版口径记 `docs/accepted-residuals.md` 在案接受。

- **图片取图改 NapCat 介导主路径（2026-10-31）**：companion 直连
  腾讯 CDN 取图间歇 ReadTimeout，图片理解仍时常失败。取图主路径
  改为 NapCat 介导：经 ActionGateway 的 `get_image` 拿 NapCat 本地
  缓存文件（本地根由配置 `media.vision.image_local_roots` 与部署根
  同级 `napcat-v4` 的 `nt_qq_*/nt_data` 自动发现组成），ImageRef
  新增 `file_id` 契约字段（段 `file` 值）供介导取图定位文件；直连
  CDN 取图保留为兜底。新增配置 `media.vision.napcat_timeout_seconds`
  （默认 8）与 `media.vision.image_local_roots`（默认空）。同轮确认
  复审补强：`get_image` 在生产 policies 显式限流 60 次/60 秒（网关
  默认 6 次会被多图回合打满主路径）；file-only 纯图在介导取图已
  接线时同样延期到视觉路由；NapCat 取图的失败收敛补全（畸形路径
  与孤立代理项 file_id、0 字节缓存按失败回落兜底、非普通文件在
  打开前以 stat 拒绝）；生产接线点与根发现纳入默认测试套件
  守卫。

- **群回复 Review 收口（2026-10-31）**：①危机不入群链补齐——群
  消息命中危机关键词时只观察登记、跳过 responder，不进 LLM 自由
  回复（此前 observe 的命中结果未参与回复门）；②CQ 字符串形
  `[CQ:at,...]` 现与数组形同口径建 AtSegment——此前字符串上报
  形态下群 @ 恒不触发、群回复静默失效；③create_app 对原始 dict
  注入的 `requests.group=approve_all` 显式拒绝（Pydantic 配置层
  之外的纵深防御）。

- **图片理解真机不触发（2026-10-31 修复）**：上线当日真机发图仍回
  固定文案。三层根因：①（决定性）本机 DNS 对公网域名返回
  198.18.0.0/15 合成地址，取图公网校验器一律判 blocked_target，
  真实图片全被跳过——校验器现按既定 P4 口径代理感知：环境代理
  适用的 URL 跳过本地 DNS 判定（字面量非公网 IP/localhost 不
  豁免），无代理时直连口径不变；②图片引用接缝契约化：ImageRef
  下沉适配器契约层、InboundEvent.images 由 normalize 双形态
  （段数组/CQ 字符串）填充，CQ 形此前参数被整体丢弃的潜伏缺口
  一并修复；③fetch() 对非 HTTPError 的 httpx 异常收敛为
  transport 结果码。新增真实报文路径回归、契约测试与门控的
  生产 config 装配自检，全套 1541 项通过。

- **N6 Claude Code Review 修复（审查方 Claude Code/DeepSeek flash，
  降级原因：用户 2026-10-31 确认 Codex 额度耗尽；findings 共
  5 组：P1×0、P2×1、P3×1、P4 nits×3，Accept 3 / Partially
  Accept 1 / Reject 1）**：P2-1 E2 读族五名门面参数名与 bundle
  schema 实证不符（file_set_id/file_id vs fileset_id/file_name/
  file_index、get_fileset_id 缺 share_code）——N1/N5 落地时
  schema 待验按臆测签名、实调必 400，终审升 verified 的
  get_share_link 亦在其中；按证据改门面签名（与 get_online_file_msg
  的 N5 修正同例）、网关补五名 _PARAM_RULES 必填、补夹具对齐
  契约测试。P3-1 L3 授权目标改结构性钉死 account（c3 不再经
  derive_target 派生，注入 group_id 无从降格），补防注入用例。
  P4-2 build_action_levels 改返回 MappingProxyType。P4-3 文档
  漂移五处（门面计数 9、facades 测试 docstring、scripts_n6
  --out 路径、登记表 docstring 的 N0 时代表述）。P4-1 裁 Reject：
  ingress 读 settings.app 兼容 dict——值路径已在 load_settings
  的 validate_app_config 校验（非法名单启动失败），残余未校验
  注入亦 fail-closed（网关构造 ValueError + frozenset 去重），
  裁决理由已注于接线处。五项不变量经审方逐项复核 holds。
  全套 1447 passed / 0 xfailed，Ruff 通过。

- **N4 Claude Code Review 修复（审查方 Claude Code/DeepSeek flash，
  降级原因：用户 2026-10-31 确认 Codex 额度耗尽；16 条 findings：
  P1×1、P2×4、P3×11，Accept 10 / Partially Accept 6 / Reject 0）**：
  P1-1 授权目标派生改按动作声明（_TARGET_KIND_BY_ACTION）——
  account/user/group 目标只取声明参数，注入的无关 group_id/
  user_id 不再改写目标（修复前群范围授权可越权执行清缓存/改
  资料/删说说等 account 动作），补参数注入反例测试；P2-1 生产
  角色解析器改用专用 identity（role:<group>）+ 放宽限流、角色
  解析失败不入缓存；P2-2 确认令牌改 peek/consume 两段，dry_run/
  角色失败等非执行结果不烧单次 nonce（角色失败用例改判并补
  「修正后同令牌可执行」回归）；P2-3 request 通道分清本地门禁与
  平台拒绝——本地门禁/simulated 抛 RequestDeferred 落 ambiguous
  + gateway_gate:*/gateway_simulated:* 细分码，不再误记
  platform_rejected 终态；P2-4 send_qzone_msg 的 canonical 路径
  主体收紧回 L2 主体集、默认 dry_run=true（与 publish_message
  前身口径对齐，legacy 工具路径不变）；P3 批：群回复产出规范化
  与过滤补全（剥转发结构/@全体、生成与发送全程异常兜底）、
  request 通道逐请求限流 identity、审计行增 authorizer、网关增
  issue_confirmation 统一签发入口、管理类文本载荷（群公告
  content 等）纳入出站复核、删除装饰性的 group_subchain 装配
  函数、架构/路线/事件覆盖文档与 DeliveryService 注释同步 N4
  口径。.handle_quick_operation 的 context 目标绑定残余风险与
  角色正向缓存 TTL 残余已在代码注释中文档化（Partially Accept）。

- **N2 Codex Review 修复（8 条 findings 全部成立或部分成立）**：
  TextSegment 正文含 `[CQ:` 时强制数组报文防注入成真段（旧字符串
  回退通道同守）；Gateway 对新消息动作做派发前内容复核（token 不
  等于内容已审，适配器直发路径同过复核）；无 token 的消息动作由
  网关按目标派生自预留（缺 token 不再免记账，get_mini_app_ark 等
  非投递动作除外）；critical 不可自声明——SendQuota 拒群域
  critical 预留、DeliveryService 段通道 critical 须显式能力开关
  （默认关，危机告警维持冻结的 send/send_alert 专用路径）；递归
  复核补齐 xml/markdown 卡片与 dict 形 json 载荷；额度 token 绑定
  计费域与目标（user 域预留不得冒充群发、群 token 不得跨群）；
  段级规划中 reply/at 作下一单元前缀不独立成条；序列化分发器
  物化输入防迭代器二次遍历丢段。回归测试 16 项
  （`tests/test_review_fixes_n2.py`），全套 1016 passed。

- **Codex 补审清零（P4–P8 及两次合并共 7 笔）**：P4–P8 当时因 Codex
  不可用以 Claude Code 降级 Review 收口，2026-10-31 以 Codex
  （gpt-6.1-sol / high）逐笔补审，共 25 条 findings + 2 条附带项，
  逐条裁决后在 `ai/qqbot-framework-v1` 修复（本段以下各提交）：
  插件注册原子性与 load 记账、状态机调用 ID 校验、注入 brain 的
  tool_loop 上限对齐；EXEMPT 落终态去重记录、in_flight 撤历史投影、
  accepted 记账与代次推进串行化；密钥读取脱敏 helper 与搜索三道
  防线接入 SecretProvider 名册；关怀 fence 与槽后 preflight（撤回
  围栏覆盖发送门内等待窗口）、调度器运行代次；部署覆盖变量贯通
  （@RUNTIME@ 与非默认路径 Environment 注入）、TimeoutStopSec 提至
  30、fingerprint drop-in 严格断言、sync 符号链接/真实路径防护、
  停机门改读 ActiveState；观测诊断值规范化（正文不得借工具名/
  嵌套值入日志与指标）、指标基数上限补全、/metrics 渲染成败
  gauge、TransportGate 指标口径限定（残留 R26）。合并一「claim
  同步阻塞」一条维持残留 R20 的既有显式接受（Stage 0 原子性
  设计），未改代码。全套 905 passed / 0 xfailed、Ruff 通过。
  本批修复位于 tag `v0.9.0`（ae32a08）之后，tag 未移动。

（框架线切现网与真机冒烟为后续独立事项，见
`docs/accepted-residuals.md` 与 `docs/deployment-runbook.md` §5。）

## [0.9.0] - 2026-10-31

框架化 P0–P8 全部收口的首个框架版本（本地 tag `v0.9.0`）。
**未切现网**：现网仍运行多气泡线 `4d2dec0`；真机冒烟未留痕，
按发布约定不打 `v1.0.0`。

### Added

- 框架化 P8 收口（分支 `ai/qqbot-framework-v1`）：
  - §5.10 工具诊断统一观测出口落地（P7 裁决留 P8）：新增
    `companion/pipeline/observers.py`（`StageMetrics` 进程内聚合、
    `MetricsObserver`、`DiagnosticsLogObserver`，只依赖标准库）；
    Respond Stage 从 provider 客户端收割 `last_tool_diagnostics`
    进 `EventContext.tool_diagnostics`，observer 经白名单投影逐条
    写 events.jsonl（`event_type="tool_step"`，带 trace_id）并汇入
    聚合；新增 `/metrics` 文本端点（恒注册、仅回环可达为部署
    口径）与 `SendQuota.snapshot()` 只读余量。口径限定：token
    只统计工具循环 respond 步（指标名带 `tool_loop` 前缀）、
    用户键为进程盐 HMAC、「估算成本」无价格数据源未实现——见
    `docs/accepted-residuals.md` R23–R25 与 runbook 附录 A。
  - 收口审计：登记册 15 条逐条复核全部关闭（登记册末尾加 P8
    复核与 §11 结案注记）；方案 §11 未收敛点（v4 接入层契约）
    以 P0 契约用例（参数化收集 26 例）与 P3 饱和测试（11 例）
    现行全绿结案，「未经 Codex 复核」的程序事实与补审债务
    分开记录。
  - 全局已接受残留与取舍清单成文：`docs/accepted-residuals.md`
    （框架线 R1–R25、进程内状态摘要、§11 结案、多气泡线单列）。
  - 文档收口：README 进度/去重口径/扩展点、architecture 可观
    测性一节、roadmap P8 行、runbook 附录 A 落地状态与 §5.1
    补审笔数（六笔→七笔）、state-ownership 新增 StageMetrics
    登记。
  - 全量终验：全套 859 passed / 0 failed / 0 xfailed，Ruff
    通过；重点子集（P0 剧本/golden/契约、P3 饱和、P5 记忆与
    投递、登记册用例）复跑 96 passed。
  - Review：Codex 额度用尽（恢复当日 22:42），按降级规则用
    Claude Code（DeepSeek flash、思考 max、只读）执行：第 1 轮
    无 P1，P2×1、P3×6、P4×6，逐条经协调者核查后 Accept /
    Partially Accept 并处置——已修：Prometheus 标签 CR 转义
    非法（P2-1）、收割诊断非序列值守卫（P3-1）、标签基数上限
    （P3-2）、用户哈希改进程盐 HMAC（P3-4 部分）、token 指标
    改名限定 tool_loop 口径（P3-5）、补 emergency 危机计数 /
    health 关闭时 /metrics 恒注册 / runner observer 不阻断 /
    非序列诊断四类测试（P3-6/P4-5）、端点渲染兜底与降级
    warning（P4-1）、成功率属性加锁（P4-2）、白名单常量合并
    （P4-3）、snapshot 不遍历用户桶（P4-4）；以文档口径裁决：
    §5.10 出口范围为工具循环诊断（P3-3）、/metrics 恒注册的
    回环部署假设成文（P3-4 部分）。第 2 轮复核：第 1 轮 13 条
    逐条 CLOSED；新增 P3×1（残留清单 R21 影响列与配置探针
    不符——pipeline 键写进 config.yaml 会被未知键校验在启动
    时拒绝、生产路径不可配置，已按实测更正）、P4×2（基数
    上限与端点降级路径缺测，已补 2 例）、轻微×2（残留清单
    §1 标题与 runbook §5 的 P0–P7 表述，已改 P0–P8）；修复后
    全套 859 全过自验收敛（降级上限 2 轮，不再开第 3 轮，
    修复 diff 随本段补审债务一并待 Codex 复核）。
  - **Codex 补审债务完整清单（共 7 笔，均待额度恢复后以
    sol/high 补跑）**：P4 `f03fc60..1af8a22`、P5
    `f03fc60..2343c9a`、P4×P5 合并 `f03fc60..b2c2bdd`、P6
    `b2c2bdd..9cb3a7d`、P7 `b2c2bdd..9fcdad3`、P6×P7 合并
    `b2c2bdd..60f6d51`、P8 `60f6d51..<P8 收口提交>`。

- 框架化 P6×P7 合并（分支 `ai/qqbot-framework-integration2`）：
  P6（`9cb3a7d`，调度与关怀接线）与 P7（`9fcdad3`，部署资产与
  发布规范）同以 P4×P5 合并线 `b2c2bdd` 为基线并行分叉，本次
  合流。唯一内容冲突为 `docs/architecture.md`：结构以 P7 全量
  重写为准，P6 的 scheduler 已接线事实回填其包结构行与文末
  差异表，旧 W1 模块表随重写退役；另同步更正 roadmap、runbook
  中「P6 未并入本线」的时点表述。合并 Review 因 Codex 额度用尽
  降级为 Claude Code（DeepSeek flash、思考 max）执行，Codex
  补审待补；降级 Review 结论 NOT CONVERGED（无 P1；P2×1、P3×2、
  P4×5），逐条核查后全部成立并已修复（扩展点表与 roadmap 归档
  口径对齐、补审计数统一指向六笔清单、architecture 告警路径
  描述按 `send_alert` 实际口径更正、runbook §4.6 死引用改指
  memory-delivery、P0 计数 502/510 加注、Unreleased 重复
  `### Added` 标题合并、CareSendGate docstring 与入口文档
  W1 表述更新），修复见合并后的 fix 提交。**Codex 补审债务完整清单（共 6 笔，均待额度恢复后
  以 sol/high 补跑）**：P4 `f03fc60..1af8a22`、P5
  `f03fc60..2343c9a`、P4×P5 合并 `f03fc60..b2c2bdd`、P6
  `b2c2bdd..9cb3a7d`、P7 `b2c2bdd..9fcdad3`、本合并
  `b2c2bdd..<本合并提交>`。
- 框架化 P6 调度与关怀接线（分支 `ai/qqbot-framework-scheduler`）：
  - Scheduler 端口与进程内实现（`companion/scheduler`，叶子包零
    companion 依赖）：命名 runner 注册/启停/状态，runner 异常只记
    类型名不杀循环；stop 改「先 cancel 再以 `asyncio.wait` 限时
    收拢」（Review P2-1：`wait_for(gather)` 对持续吞取消的 runner
    不构成硬上限，实测复现后改）。连接健康迁为调度任务（`run()`
    整体注册、延迟到 lifespan 构造，语义不变），关怀触发同由调度
    托管；调度注册在 create_app 组合根完成，lifespan 只启停、可
    重复进入（Review P3-4）。
  - 同意控制面补齐：关怀控制命令（开启/暂停/停止/关闭关怀提醒，
    精确匹配）接入 Admission 前置分支（judge 与限流之前），顺序
    固定为「先持久化撤回 → 使待发关怀失效 → 再回复确认」，与忘掉
    分支共用 `_state_locks` 线性化（Review P3-2）；仅名单内用户有
    命令面，未接线时命令面不存在、行为与接线前等价。opt-in 30 天、
    pause 7 天（文案不写死天数，Review P4）。
  - KD-S03 关闭（登记册 15 条至此全部关闭）：「忘掉」在同一临界区
    内连带清关怀同意并失效待发关怀，确认文案不变。
  - CareScheduler 待发队列：项带 epoch 与 delivery generation 戳
    记，撤回/暂停/忘掉经 invalidate 失效，用户新消息推进代次后未
    发关怀自动失效；接线代次校验时缺戳记的项 fail closed 丢弃
    （Review P3-1）；发送一去不回、永不重试。
  - CareSendGate 接线共享 `processor.delivery`（与回复路径同一
    DeliveryService/额度账本，落实 P5 注记）；`current_care_safety`
    发送前实时重查（revision 取 safety epoch）。关怀阻塞用独立
    时钟 `_last_crisis_at`（每次危机事件在任何 await 之前同步
    推进，应急兜底路径同样推进；已登记 state-ownership）：Review
    第 1 轮 P2-2 指告警 await 窗口内关怀可能误发，第 2 轮复核又
    发现初版修法（把占位记录写进 crisis_history）会刷新告警去重
    锚点、令持续危机在窗口后不再重复告警，故两时钟拆开记账，
    crisis_history 恢复基线语义，并补告警节奏回归用例。
  - 配置：`proactive_care` 扩为 `ProactiveCareConfig`（新增
    `store_path` / `check_interval_seconds`，example 同步防漂移）；
    默认关口径不变（D2），store_path 未配置时关怀面不装配，相对
    路径以 settings.root 为基准、仓内路径由 CarePreferences
    fail-fast 拒绝。触发节奏参数属 D2 另立阶段，本阶段接线本身
    不产生任何发送。
  - 测试：新增 `tests/test_scheduler_p6.py`（18 条）与
    `tests/test_care_wiring_p6.py`（17 条，含 create_app 组合根
    接线断言、lifespan 重入与告警节奏回归）；全套 817 passed /
    0 xfailed，Ruff 通过。Review 为降级轮次：Codex 额度用尽
    （恢复当日 22:42），改用 Claude Code（DeepSeek flash、思考
    max、只读）2 轮——第 1 轮 P2×2、P3×5、P4×7 与第 2 轮复核
    发现的 P2×1（初版修复引入的告警锚点漂移）、P3×2、P4×3 均已
    逐条核查修复并补回归用例（第 2 轮 findings 的修复按降级上限
    未再开第 3 轮复核，随本 diff 一并计入 Codex 补审债务；另有
    2 条 P4 以注记/文档方式处置、1 条同步 SQLite 阻塞项记为已知
    残留留待后续硬化）；Codex 补审待补（本条目落笔时与 P4、P5、
    合并计 4 笔；其后 P7 与 P6×P7 合并续增，完整清单共 6 笔见
    本段顶部合并条目）。

- 框架化 P4×P5 双线合并（分支 `ai/qqbot-framework-integration`）：
  - 合并提交 `ced5c6a`：工具插件线与记忆/投递线汇合，冲突逐项
    按「投递/记忆口径从 P5、插件/状态机从 P4、brain 双保留」裁决；
    `build_plugin_registry`/`WebSearchPlugin` 新增 `secret_provider`
    注入并穿透至 `search()`，`DeepSeekClient.tool_turn` 改经
    provider 取密钥。全套 778 passed / 0 xfailed，Ruff 通过。
  - 合并 Review（Codex 额度用尽，降级 Claude Code + DeepSeek
    复核）P3 级修复：登记册 KD-S03 的 `ShortTermMemory.forget`
    引用更新为 `ConversationStore.forget`；import 边界图 safety
    行按实测更正（顶层依赖 config），`brain` 不依赖 `tools` 转为
    断言；`PluginRegistry.unload_all` 单插件异常隔离，lifespan
    装载失败时先卸载再上抛；新增注入链回归测试
    `tests/test_secret_wiring_integration.py`。Codex 补审待补
    （与 P4 `f03fc60..1af8a22`、P5 `f03fc60..2343c9a` 两笔一并，
    等额度恢复后跑）。
- 框架化 P7 部署资产与发布规范（分支 `ai/qqbot-framework-ops`，
  基线 `b2c2bdd`，不改 companion 业务代码、现网零触碰）：
  - `deploy/` 入仓：`systemd/` 三份单元模板（`@BASE@` 占位渲染，
    与现行 `runtime-24h/` 单元逐行等价，由
    `tests/test_deploy_assets.py` 锁死）；`runtime/` 四件套
    （restore-service.sh 行为对齐仓外版并支持 `QQBOT_BASE` /
    `QQBOT_RUNTIME` / `QQBOT_SYSTEMD_DIR` / `QQBOT_FINGERPRINT_DIR`
    覆盖，companion 单元改为渲染实体安装——主要有意差异之一，
    全部差异逐项列于脚本注释与 `deploy/README.md`；companion-launch /
    napcat-watchdog / fingerprint-namespace 路径改环境变量、缺省
    等价、凭证文件支持 `QQBOT_CREDENTIALS_FILE` 覆盖）；
    `bin/health-check.sh`（纯只读七项自检）与
    `bin/sync-to-runtime.sh`（默认 dry-run、`--apply` 落盘前逐个
    备份、指纹/凭证/日志永不同步）。
  - `Dockerfile` 增加非 root 用户 `appuser`（uid 10001）与
    `/health/ready` 的 HEALTHCHECK，原构建与 CMD 语义不变。
  - 新增 `docs/deployment-runbook.md`（健康检查/换机恢复/常规
    发布/数据库规程/P8 切现网检查单/§5.10 裁决附录/一致性自查
    附录）；`docs/architecture.md` 按框架结构全量重写（W1/
    relay/白名单等已删除概念清理并逐项标注）；`docs/roadmap.md`
    改写为 P0–P8 框架状态页。
  - restore 隔离演练记**未验证**：本机能力集无 CAP_SYS_ADMIN /
    CAP_MKNOD，userns 内 proc/sysfs 挂载被宿主锁定掩码挂载阻止，
    debootstrap 与 systemd-nspawn 均实测失败，无可用隔离环境；
    证据见 runbook §2.6。替代核对：health-check.sh 对现网只读
    运行 7 项全 PASS、仓库版 restore `--check` 只读通过。
  - §5.10 工具诊断统一观测出口裁决：不在 P7 落地，留 P8
    （理由与待办见 runbook 附录 A）。
  - 本阶段 Review：Codex 额度用尽（恢复当日 22:42），按降级规则
    用 Claude Code + DeepSeek（flash/max、只读）执行 1 轮：
    1×P1、4×P2、13×P3、15×P4，核心安全语义（拒绝门/指纹校验/
    `--check` 零更改/模板哈希等价/密钥纪律/未验证口径）逐项判
    干净；有效项已修复并重测（restore 隔离目录激活守卫与单一
    来源模式、运行时脚本按自身位置推导 base、sync 模式漂移修复、
    golden 单元入仓使等价断言无条件执行、Dockerfile 补 venv
    PATH、`.dockerignore` 排除生产配置、runbook 回滚/WAL/损失面
    等多处补齐），全套终验 809 passed / 0 xfailed、Ruff 全过。
    Codex 补审待补（`b2c2bdd..9fcdad3`；完整补审清单共 6 笔见
    本段顶部 P6×P7 合并条目，等额度恢复后跑 sol/high）。

- 框架化 P0 基线验收产出（基线 `151fff5`，均不改产品代码）：
  - 黄金报文夹具 `tests/fixtures/golden/`（28 个）与
    `tests/test_golden_payloads.py`、`tests/test_adapter_contract_p0.py`：
    v4 TransportGate 契约用例，32 passed / 22 xfailed；
  - 对话剧本 `tests/scripts_p0/`（7 文件 15 剧本）与
    `tests/test_dialogue_scripts_p0.py`：13 passed / 3 xfailed；
  - 已知差异登记册 `tests/KNOWN_DIFFERENCES_P0.md`：共 15 条
    （KD-001、KD-C01～KD-C11、KD-S01～KD-S03），可测差异一律以
    `xfail(strict=True)` 登记，未处置差异不得带入 P8；
  - 登记用例 `tests/test_known_differences_p0.py`（KD-S01、KD-S02，
    2 xfailed）与 import 边界检查骨架
    `tests/test_import_boundaries_p0.py`（只断言实测成立的依赖规则，
    P3 起强制的目标规则清单见该文件 docstring）；
  - `pyproject.toml` 追加 coverage 配置骨架
    （`[tool.coverage.run]` source=companion、branch=true；
    `[tool.coverage.report]` fail_under=0，P3 起逐步抬升）；
  - 本文件（CHANGELOG）与上述发布/tag 约定。
- P0 验收口径：前三路产出時点全套 493 passed / 25 xfailed /
  0 failed；本段全部合入后最终全套为 **502 passed / 27 xfailed /
  0 failed**（增量恰为本路新增的 9 条 import 通过用例与 2 条登记
  xfail，无其他漂移）。（注：502 为本段合入时点计数，其后经主管
  复验，P0 阶段最终全套以 **510 passed / 27 xfailed** 为准，
  roadmap 与本文件其余处均按 510 记。）
- 框架化 P1 配置与密钥形式化（分支 `ai/qqbot-framework-config`）：
  - `companion/config.py` 引入 Pydantic 配置模型树 `AppConfig` 与
    fail-fast 校验：未知键/类型错误/非法组合在启动期报错并指出键
    路径（不回显配置值）；`config.example.yaml` 补齐
    `napcat_base_url` 与动作工具字段，与模型叶子字段集做等值防漂移
    契约（`tests/test_config_p1.py`）；
  - 现网配置的脱敏副本夹具
    `tests/fixtures/config_p1/production-sanitized.yaml`（真实 QQ 号
    替换为合成 ID）逐叶子等价加载测试；只读策略在现网省略的
    `rate_limit`/`window_seconds` 由模型补齐旧消费端默认值 6/60；
  - 名单新名 `care_known_user_ids` 作为输入别名兼容读（与旧键
    并存时必须一致）；`Settings.app` 保留旧字典兼容视图，正式
    模型树在 `Settings.config`；
  - 密钥经 `SecretProvider`/`EnvironmentSecretProvider` 按名从
    进程环境读取，`Settings` 与 provider 的 repr 不再包含密钥值；
    `.env` 密钥忽略并告警的旧纪律不变；
  - 热更新白名单（仅功能开关）与 `ConfigReloader` + JSONL 审计行
    （只记键路径）落地为组件（见 `docs/config.md`），运行进程
    尚未接线周期性重读，生产行为不变。
  - P1 补审（Codex Review 对撞后修复）：告警工具模型默认值对齐
    旧消费端有效语义（enabled 缺省 True、dry_run 缺省 False，
    ALERT_QQ 闸门仍在 ingress）；YAML 预检错误路径改为只沿
    schema 结构拼名，未知字段之下的数据键名不再进错误与审计；
    名单字段改为 tuple 防原地篡改；example 防漂移契约补默认值
    维度（有意覆盖须登记）；`SecretProvider` 服务范围在文档中
    明确收窄为启动检查；weather 补默认 `fetch()` 经
    `asyncio.to_thread` 委派的确定性覆盖。
- 框架化 P4 工具插件化（分支 `ai/qqbot-framework-tools`）：
  - `companion/tools/plugins.py` 落地 ToolPlugin 协议与注册表：
    PluginMeta（name/version/capabilities/allowed_domains/
    max_output_chars）、独立配置命名空间（Pydantic 闭合校验）、
    register/on_load/on_unload 生命周期；插件经 PluginContext 只能
    登记一个同名只读工具，拿不到 identity/sender/gateway，门控
    （双开关/闭合校验/超时/截断/隐私挂钩/全局信号量）仍由
    ReadOnlyRegistry + ToolGateway 统一执行；
  - 现行三工具迁为插件（CurrentTime/Weather/WebSearch），结果
    政策按 §5.5 类型化声明：时间/天气 direct 直出（天气署名随
    结果原样保留）、搜索 synthesize（内容不可用哨兵与不可用
    文案为终结结果、正文回喂置待合成须经 marks_synthesis 显式
    声明）；网络插件须声明 allowed_domains 并向宿主申报固定
    端点（宿主注册时校验、每次调用前复检；这是对自报端点的
    预检而非网络沙箱，插件为同进程可信代码）；插件
    max_output_chars 接管结果截断上限；
  - `LLMProvider` 能力声明替代 isinstance：`supports_tools`
    为唯一判据，未声明的 provider 走普通生成降级；DeepSeek 为
    生产实现（单轮原语 tool_turn），EchoStub 为测试实现；
  - 工具循环显式状态机 `companion/tool_loop.py`（Respond →
    ToolCall → ToolInvoke → Synthesize），轮次/调用上限来自
    ToolLoopLimits 配置对象（现行 ≤2 轮、≤3 次调用）；配置侧
    同步落地 `llm.tool_loop` 正式模型（`ToolLoopConfig`，含
    example 与 docs/config.md，越界在加载期报错），ingress 从
    模型树读取注入；每步落结构化诊断（模型名/工具名/结果码/
    耗时/token，不含正文）；TOOL_DATA_INSTRUCTION 上升为
    框架级契约并逐字锁定；
  - `examples/dummy_tool_plugin.py` 最小插件示例；契约测试
    `tests/test_plugins_p4.py` 覆盖未注册工具名、超轮次/
    超调用上限、域名未声明与端点越界、插件异常收敛、
    supports_tools=false 降级、dummy 插件端到端与诊断脱敏；
    既有工具测试的注册 helper 改经插件登记（断言不变）。
  - P4 阶段 Review（Codex 额度耗尽，按降级规则改用 Claude Code
    + DeepSeek 复核）后等价性修复：畸形 tool_calls 恢复
    calls_seen 先置位语义（回退不写短期记忆）；状态机复核
    provider 调用列表形态/上限；未启用天气恢复固定文案终结；
    插件生命周期由 app lifespan 托管装载/卸载；诊断入口清空
    防跨请求残留；渲染回调返回值校验；PluginContext 收窄为
    登记回调（不暴露注册表本体）。
- 框架化 P5 记忆与投递解耦（分支 `ai/qqbot-framework-memory`）：
  - 记忆拆分：`ShortTermMemory` 单体拆为同库不同表的
    `ConversationStore`（历史/onboarded/人设包偏好）与
    `DeliveryLedger`（投递状态机 + 代次），共享 `Database`
    （WAL、schema_version=1、单一迁移机制）；台账迁移与历史
    投影同一事务，ambiguous 前缀不进历史（见
    `docs/memory-delivery.md`）。
  - claim 落库（D3）：接受边界改用 `SqliteClaimStore`，代次与
    豁免计数落库、上代 processing 转 abandoned 不重放；
    已知差异 KD-C08 关闭，登记册 xfail 清零（见
    `tests/KNOWN_DIFFERENCES_P0.md`）。
  - 投递契约收敛：新增 `DeliveryService` 独占额度预留与记账，
    回复/危机/告警/关怀四路发送统一经它；`CareSendGate` 解除对
    `HttpOneBotClient` 的硬依赖、关怀改走 DeliveryService；
    OneBotAdapter 透传 DeliveryHandle 的预留 token。
  - claim 纯定义下沉新叶子包 `companion/domain`，import 边界
    登记同步更新（memory → config/domain、tools → config/quota）。
  - `SecretProvider` 运行期接线（P1 遗留关闭）：DeepSeek /
    OpenAI-compatible / Tavily / Gmail 发送均改经 provider
    按名取密钥，读取时机与缺 key 回退不变。
  - 状态归属登记表落地 `docs/state-ownership.md`；迁移守恒、
    故障注入（两写间崩溃、投递在途忘掉）与快照回滚演练测试
    新增于 `tests/test_memory_p5.py`（回滚损失面已在
    `docs/memory-delivery.md` 写明）。全套 746 passed /
    0 xfailed，Ruff 通过；现网服务与生产配置未动。
