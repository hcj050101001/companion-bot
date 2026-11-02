# 配置与密钥（P1）

## 模型与加载顺序

应用 YAML 在启动时先由 `companion.config.AppConfig`（Pydantic）校验，
再进入运行期：

1. 模型默认值；
2. `config.yaml`（`CONFIG_PATH` 指定时以该文件为准，缺失时回退
   `config.example.yaml`，与旧行为一致）；
3. 进程环境覆盖——目前只有 `NAPCAT_BASE_URL` 这一个非密钥项；
4. 密钥永远不走 YAML，只由 `SecretProvider` 从进程环境按名读取。

未知键、类型错误、空的必填名单、延迟区间倒置等都会在启动期以
`ConfigValidationError` 失败，错误消息只包含键路径与原因，不回显
配置值。YAML 解析失败（语法、非法标签标量）同样被收敛为只带键
路径或行列位置的 `ConfigValidationError`；重复映射键在解析前即
被拒绝并指出其路径（PyYAML 默认的“后者覆盖前者”会绕过 fail-fast）；
`!!set`/`!!pairs`/`!!omap` 这类集合标签（其“键”是数据值、不是
配置键名）与递归别名在解析前整体拒绝，只报所在配置路径。错误
路径只沿 schema 已知的结构层拼键名：未知字段本身会被点名（与
Pydantic 对 extra 键的口径一致），但未知字段之下、本应是标量或
列表的位置上出现的映射，其键属于数据内容，不再拼进路径，只报
最近的安全路径或行列位置——避免误粘成键名的敏感数据随错误消息
与热更新 rejected 审计泄漏。
`llm.provider` 只接受 `deepseek`、`openai_compatible`、
`codex` 三个既有值；`codex` 仍受 `ALLOW_INSECURE_CODEX=1` 闸门约束。
`llm.tool_loop.max_tool_rounds`（默认 2，0–8）与
`llm.tool_loop.max_tool_calls`（默认 3，0–32）是工具循环状态机
（方案 §5.4）的轮次/调用上限，由 ingress 从正式模型树读取后注入
provider；越界值在配置加载期即报错，不留到运行时。

迁移期兼容：

- `Settings.config` 是已补齐默认值的正式模型树；
- `Settings.app` 保留旧字典视图，只含文件/环境中显式出现的键
  （另加旧 loader 本来就补的 `llm.provider` 默认值），未迁移模块的
  `.get(..., 默认值)` 语义不变；
- 已知用户名单的旧键 `allowed_private_user_ids` 继续作为正式字段；
  新名 `care_known_user_ids` 可作为输入别名兼容读，两键并存时必须
  一致，加载后旧字典中两键都会出现且指向同一份名单。正式树中
  名单存为 tuple（`frozen` 挡不住 list 的原地修改），消费端需要
  list 时拷贝；
- 告警工具 `tools.alert_private_msg` 的模型默认值与旧消费端的
  有效语义一致：`enabled` 缺省 True、`dry_run` 缺省 False。实际
  发送另受 ingress 处的环境闸门约束——未设置 `ALERT_QQ` 时
  ingress 会把 enabled 与门为 False（与 `NAPCAT_BASE_URL` 同属
  环境层覆盖，不属于文件配置）。

## Example 防漂移

`config.example.yaml` 的叶子字段集与 `AppConfig` 的叶子字段集由
`tests/test_config_p1.py` 做等值契约：任一侧新增/删除字段而另一侧
未同步，测试即变红。example 中的 `allowed_private_user_ids` 是
占位空名单，契约测试会换成合成 ID 后再做完整模型校验。此外还有
默认值契约：example 的每个叶子值须与最小配置的模型默认值相等，
有意不同者必须在测试的 `_EXAMPLE_DEFAULT_OVERRIDES` 中登记
（目前只有 `llm.display_name` 与告警工具的 `enabled`/`dry_run`），
模型默认值被悄悄改动同样会变红。

## read_only_tools.policies.date_countdown（倒计时 / 纪念日）

- `read_only_tools.policies.date_countdown.enabled`（默认 `false`）：
  只读工具 `date_countdown` 的 policy 开关；须与全局
  `read_only_tools.enabled` 同开才暴露。仅此叶子可热更新（已加入
  `HOT_RELOADABLE_CONFIG_PATHS`），其余叶子变更须重启。
- `read_only_tools.policies.date_countdown.rate_limit`（默认 `6`）与
  `read_only_tools.policies.date_countdown.window_seconds`
  （默认 `60`）：限流数值，与既有只读工具 policy 默认值相同，
  变更须重启。
- `read_only_tools.policies.date_countdown.anniversaries`
  （默认 `[]`）：纪念日清单，最多 50 条，变更须重启。每条为严格
  模型（未知键 fail-fast），有两种互斥形态：
  - 形态 A（年度型）：`name` + `month`（1–12）+ `day`（与 month
    组成的日须在闰年语境下合法，2 月允许 29 日、至多 29 日，
    4/6/9/11 月至多 30 日），可选 `year`（1900–2100，起源年份，
    用于周年数）；
  - 形态 B（一次性型）：`name` + `date`（严格 `YYYY-MM-DD` 且须
    为真实存在的日历日）；不得同时带 `year`（`year` 仅属形态 A，
    混给同样在加载期 fail-fast）。
  两形态字段不得混给、不得皆缺；条目名称经 NFKC 规范化 +
  casefold（并折叠空白）后不得重复。非法条目在配置加载期以
  `ConfigValidationError` fail-fast，错误只报键路径不回显值。
- **可见性（启用前必读）**：可见性按条目来源分。**config 静态
  条目**：列表模式（问「有哪些纪念日」看全部条目）仅主人可用
  ——工具在宿主侧以 caller_check 校验主体，非主人调用收敛为
  固定「暂不可用」文案、不回显任何条目；按名称、具体日期或
  月日单条查询维持开放口径，任何与机器人对话的用户均可查询
  （原已接受残留 R-C1 已由倒计时体验增强线关闭，见
  [accepted-residuals.md](accepted-residuals.md) §6）。因此配置
  时仍只填愿意向聊天对象公开的纪念日。**对话存储条目**（主人
  经对话记下的，见下节）是主人私密数据：仅主人可见——列表、
  按名查询、合并视图全表面一致，非主人按名查存储独有条目与
  查不存在返回完全相同的结果，不泄露存在性、日期与周年数。
- **对话式管理（主人）**：主人可在私聊里直接维护纪念日，无需改
  配置文件——「记一下：结婚纪念日 5月20日」（或完整日期
  「记一下：春节 2027-02-17」）新增/更新，「删掉 结婚纪念日」
  删除，「我记了哪些纪念日」查看。对话记录的条目落在既有共享
  库的 `anniversaries` 表（生产即 `data/memory.db`），运行时
  绝不改写本配置文件，记下即生效、无需重启；config 静态清单
  变更仍须重启。合并展示口径：以 config 清单为骨，规范化同名
  时以对话记录的条目为准（主人的显式维护覆盖部署期静态配
  置）；删掉对话条目后，同名 config 条目重新可见。非主人发送
  管理形态的消息一律固定拒绝文案，不查存储、不确认条目是否
  存在。config 条目无法经对话删除（会得到需改配置的提示）。

## media.vision（私聊图片理解）

- `media.vision.enabled`（默认 `true`）：总开关。关闭后行为与接入
  前逐字一致——纯媒体回原固定文案、图文消息走普通文本生成。
- `media.vision.model`（默认 `deepseek-flash`）：视觉模型名；
  仅当 `llm.provider=deepseek` 时装配视觉服务，其余 provider
  不装配（图片按关闭口径处理）。
- `media.vision.max_images`（默认 3，范围 1–10）：单轮最多附带
  的图片数；超量在 prompt 内以系统注记向模型说明。
- `media.vision.max_image_bytes`（默认 10485760 = 10 MiB）：单图
  下载字节上限，流式截断，超限跳过该图。
- `media.vision.timeout_seconds`（默认 30）：单次视觉生成调用
  超时；整轮图片准备另有 25 秒总截止（代码常量）。
- `media.vision.napcat_timeout_seconds`（默认 8，范围 1–60）：
  NapCat 介导取图（主路径）的单图超时。取图先经网关 `get_image`
  按 file_id 拿 NapCat 本地缓存文件、过本地根白名单校验后读
  字节；失败才回落直连 CDN 下载（兜底）。
- `media.vision.image_local_roots`（默认 `[]`）：NapCat 本地缓存
  根的显式补充名单，相对路径相对部署根解析。未配置时启动时
  自动发现部署根同级 `napcat-v4/home/.config/QQ/nt_qq_*/nt_data`
  目录作为根集合（发现只在启动时做一次，变更须重启）。
- 以上键均不在热更新白名单内，变更须重启 companion。密钥仍只
  经 `DEEPSEEK_API_KEY`（SecretProvider/进程环境），不入 YAML。

## requests（申请处置）与群回复

- `requests.friend`（默认 `manual`）：好友申请处置模式——
  `manual` 只落库待人工；`allowlist` 仅名单内自动同意；
  `approve_all` 全自动同意（2026-10-31 用户拍板的生产口径）。
- `requests.group`（默认 `manual`）：入群申请/邀请处置模式，
  只接受 `manual` / `allowlist`——配置层不开放 `approve_all`，
  全自动同意的范围只限好友申请。
- `requests.approve_user_ids` / `requests.approve_group_ids`：
  allowlist 名单（默认空）。
- 任一模式非 manual 时，组合根装配 GatewayRequestChannel
  （system 主体 + account 级授权 + 配置层常开确认）经既有状态机
  与 ActionGateway 执行 `set_friend_add_request`（approve=true）；
  该动作还须 `actions.overrides.set_friend_add_request` 显式
  `enabled: true, dry_run: false` 才真正派发；群申请若用 allowlist，
  同理须 `actions.overrides.set_group_add_request` 显式
  `enabled: true, dry_run: false`，否则处置全部落 ambiguous。
  自动模式已配置但
  发送端无网关时启动期 fail-fast，拒绝静默降级 manual。
  以上键均不在热更新白名单内，变更须重启。
- 群回复：`tools.group_chat` 须 `enabled: true`、`dry_run: false`、
  `reply_enabled: true` 三者同开才真正回复；`reply_enabled` 开时
  组合根装配默认生产 responder（仅 @ 触发、空 history、基础
  人设、不调工具、不带私聊记忆），变更须重启。

## 密钥纪律

- 密钥名固定为 `DEEPSEEK_API_KEY`、`GMAIL_APP_PASSWORD`、
  `ONEBOT_SECRET`、`OPENAI_COMPATIBLE_API_KEY`、`TAVILY_API_KEY`；
  写进 `.env` 的密钥仍被忽略并打启动警告（与旧行为一致）；
- `SecretProvider` 的服务范围包括 `load_settings` 的启动检查
  （ONEBOT_SECRET 必备、GMAIL_APP_PASSWORD 是否已配置）与运行期
  消费者（DeepSeek / OpenAI-compatible / Tavily / Gmail 发送），
  均经该协议按名取值；实现只能按名取值、不得批量导出，
  `repr` 不暴露密钥值；
- `Settings` 与 `EnvironmentSecretProvider` 的 repr 不包含密钥值；
  配置校验错误不回显输入值；
- 测试只使用合成密钥，生产 `config.yaml` 只读核对、从不由测试写入。

## 热更新白名单与审计行

可不重启变更的只有功能开关（`HOT_RELOADABLE_CONFIG_PATHS` 写死在
`companion/config.py`）：各工具/只读工具/连接健康/主动关怀的
`enabled`，以及搜索的 `content_extraction_enabled`。名单、地址、
模型、路径、限流数值等其余键变更必须重启。

`ConfigReloader` 是供后续阶段接线的重读组件：候选配置先完整校验，
再逐叶子比对；解析/校验失败与含任一非白名单变更的候选都整体
拒绝并写一行 `rejected` 审计（附出错键路径），全在白名单内才
替换并写 `ok` 审计；若 `ok` 审计写失败，替换会被回滚、下次重读
重试，不留“已生效未审计”的状态。审计行是 JSONL，只含时间、
actor、动作、结果码与键路径，不含配置值。当前运行进程尚未
接线周期性重读，生产行为不变。
