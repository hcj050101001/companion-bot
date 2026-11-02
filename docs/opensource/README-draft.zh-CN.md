<!-- 草稿 · DRAFT —— 开源化第一阶段产物，未定稿、未发布 -->
<!-- D2/D3/D7 已定案：主名 companion-bot、许可证 MIT、首发版本 v0.10.0；本稿未发布 -->

# 阿年 · companion-bot（草稿）

> 「阿年」（companion-bot）是一个基于 NapCat / OneBot v11、安全优先的 QQ 私聊 AI 陪伴机器人及轻量框架：167 个 NapCat 规范动作全量登记、经分级授权网关单一收口，危机回复永不由模型生成，投递的额度、台账与三态回执由独立投递服务独占。

[![License](https://img.shields.io/badge/license-MIT-green)](#许可证)

## 特性

| 能力 | 说明 | 默认 |
|---|---|---|
| 私聊陪伴对话 | LLM 驱动的私聊陪伴，人设可插拔（4 套人设包，含 SHA-256 校验与安全包装） | 开（default 人设） |
| 图片理解 | 私聊图片/图文经视觉模型回复（单轮 ≤3 图、单图 ≤10MiB）；取图走 NapCat 本地介导 + 直连兜底，带 SSRF 边界校验 | 开 |
| 多气泡回复 | 明确要求逐条时逐句分泡发送（上限 10 泡），带出站复核与身份披露检查 | 开 |
| 只读工具 | 当前时间、天气、联网搜索、纪念日倒计时（各自全局 + 单项双开关；纪念日可由主人在私聊对话中增删查、仅主人可见） | 开 |
| 群聊 | 群消息观察与分类；群回复 / 群发送为独立双开关 | 以生产配置为准 |
| 主动关怀 | opt-in 同意面、≤30 天过期、夜间静默、发送前双重重查、全 fail-closed | **关**（接线完成，未启用） |
| NapCat 全接口面 | 191 条动作逐项登记处置，167 个规范动作全量接入（见下） | 按分级门禁 |

## 架构

消息进入后依次经过 **11 个命名 Stage**（顺序即契约，改序测试即红）：

```
TransportGate → Normalize → Admission → SafetyJudge → InboundRateLimit
→ Command → MemoryLoad → Persona → Respond → OutputGuard → Deliver
```

两个独占语义的服务（不经插件/应用层绕过）：

- **ActionGateway**：一切出站 NapCat 动作的单一收口——主体校验 → 分级门禁（L0–L3）→ 逐资源授权（不可逆动作须一次性确认令牌）→ 幂等账本防重放 → 白名单字段审计（凭证永不入日志）。业务代码只能经 9 个类型化门面调用它。
- **DeliveryService**：聊天投递的额度与并发唯一记账方，两层额度（单目标 + 账号总预算，危机走独立 critical 通道），台账与对话历史同事务落盘，回执三态（accepted / rejected / ambiguous，ambiguous 永不重发）。

框架采用八层目标架构（apps / adapters / pipeline / domain / services / ports / infra / config）；其中流水线层已完整落地，其余层按契约逐步迁入，现状差异如实记录在 `docs/architecture.md`。

### NapCat 动作注册表

以本机 NapCat 4.18.41 官方 bundle 三方对账为准，全表 **191 条**全部终局处置：

- **167 个规范动作全量接入**：L0 只读 61 / L1 常规 62 / L2 管理 36 / L3 高危 8（L3 默认硬禁，仅运维名单 + 重启可受控解锁）
- 其余 24 条逐项写明理由（内部保留 6 / 不支持 4 / 运维专用 2 / 已弃用 2 / 待验证 2 / 别名与不存在等）
- 入站事件 32 类全覆盖（仅私聊进入陪伴链，群链只观察）；消息段 23 类登记、21 类可发送

## 安全设计

- **危机处置永不由模型生成**：关键词 + 模型 judge 双路判定，任一命中即走固定话术模板（热线 12356 / 110 / 120）；judge 出现任何异常一律按疑似危机保守处理。
- **安全判定不可绕过**：SafetyJudge 位于限流之前、流水线固定位置；人设包文本永远包在固定安全包装之内。
- **分级与授权**：L2 动作逐资源授权；不可逆动作一次性确认令牌；L3 默认硬禁。
- **隐私与日志纪律**：日志与指标只记元数据（结果码、摘要、字节数），不记消息正文、图片内容、URL 与凭证；「忘掉」即真删。
- **身份披露**：出站回复带身份披露检查，机器人不冒充真人。

## 快速开始

前置：Python 3.12+、`uv`、一个可扫码登录的 QQ 号、DeepSeek API key。

1. 克隆本仓库，`uv sync` 安装依赖。
2. `cp config.example.yaml config.yaml`，把 `allowed_private_user_ids` 改成你自己的 QQ 号。
3. `cp .env.example .env` 并填入非密钥项；必填密钥改为以进程环境变量提供：`ONEBOT_SECRET`（自设随机串）、`DEEPSEEK_API_KEY`；可选 `TAVILY_API_KEY`（联网搜索）。密钥只从进程环境变量读取——写进 `.env` 的密钥会被忽略并告警。
4. 按 NapCat 官方说明启动 NapCat（本机或 Docker），用 QQ 扫码登录。
5. 在 NapCat WebUI 中配置 OneBot 接入：HTTP API 与事件上报地址指向 companion（默认 `http://127.0.0.1:8090/onebot/event`），token 与第 3 步的 `ONEBOT_SECRET` 保持一致，消息格式选数组（array）。
6. 启动 companion：`uv run uvicorn main:app --host 127.0.0.1 --port 8090`。
7. 用白名单内的账号私聊机器人发一条消息验证回复；发一张图片验证视觉回复。

> 生产部署（含 systemd 守护、断线恢复、备份口径）见 `docs/deployment-runbook.md`；该手册含特定环境路径，通用化安装指南在开源化路线图中（见下）。

## 测试

- `scripts/test.sh`（或 `uv run pytest -q --timeout=10`）+ `uv run ruff check .`
- 测试资产：1618 个测试函数、55 个 golden 报文契约夹具、对话剧本测试、流水线顺序/饱和测试、注册表三方对账测试；全量套件实测 3025 passed / 4 skipped、总覆盖率 90.3%（门槛 `fail_under`=85）。
- CI 在每次 push / PR 运行测试、ruff 与 gitleaks 扫描。

## 文档索引

- 架构：`docs/architecture.md`
- 配置：`docs/config.md`、`config.example.yaml`（与代码有防漂移契约，是可信配置文档）
- NapCat 集成收口：`docs/napcat-integration-closeout.md`；事件/消息段覆盖矩阵：`docs/napcat-event-coverage.md`、`docs/napcat-segment-coverage.md`
- 部署手册：`docs/deployment-runbook.md`
- 路线图与阶段状态：`docs/roadmap.md`；变更史：`CHANGELOG.md`

## 路线与非目标

- 路线：通用化安装指南（现安装稿仍为草稿、含环境相关路径待通用化）、公开仓 CI 徽章（待公开仓首跑后补）。
- 明确不做：插件市场、多平台产品化、代码执行工具——本项目窄而专，陪伴是首个且唯一的产品场景。

## 许可证

MIT。
