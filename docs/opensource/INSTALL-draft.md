<!-- 草稿 · DRAFT —— 开源化第二阶段产物，未定稿、未发布 -->
<!-- 本指南只基于暂存树中实际存在的部署资产编写；所有路径均为模板化占位（<repo-root>、$HOME 等），不含任何本机绝对路径、真实密钥、QQ 号或内部信息。 -->

# 安装指南（草稿）/ Installation Guide (Draft)

> 主语言为中文，关键命令与路径与英文 README 草稿（`README-draft.en.md`）一致。
> 本项目由两部分组成：**companion**（本仓库，Python + FastAPI）与 **NapCat**（OneBot v11 协议端，独立项目）。两者通过 HTTP 互联：NapCat 把事件上报到 companion 的 `/onebot/event`，companion 通过 NapCat HTTP API 发动作。

## 0. 前置条件

- Python 3.12+（`pyproject.toml` 要求 `>=3.12,<3.14`）与 [`uv`](https://docs.astral.sh/uv/)
- 一个可扫码登录的 QQ 号（机器人账号）
- DeepSeek API key（默认 LLM provider）；可选 Tavily API key（联网搜索）
- 扫码登录、付费等事项不在本指南自动化范围内，均由部署者本人完成

## 1. 通用准备（三条路径共用）

```sh
git clone <repository-url> companion-bot
cd companion-bot          # 以下 <repo-root> 指此目录
scripts/install.sh        # = uv sync，并从示例生成 .env / config.yaml（已存在则不覆盖）
```

手动等价步骤：

```sh
uv sync
cp .env.example .env
cp config.example.yaml config.yaml
```

配置要点（以暂存树实际文件为准）：

- `config.yaml`（由 `config.example.yaml` 复制）：把 `allowed_private_user_ids` 填为你自己的 QQ 号（必填、非空，否则生产启动 fail-fast）；其余开关先保持示例默认（群聊、QQ 空间、好友管理、只读工具、主动关怀等默认关，需要时再逐项开启）。完整字段说明见 `docs/config.md` 与 `config.example.yaml` 本身（与代码有防漂移契约）。
- 密钥纪律：`ONEBOT_SECRET` 与 `DEEPSEEK_API_KEY` **只能**通过进程环境变量提供，写进 `.env` 或配置文件会被忽略并告警：

```sh
export ONEBOT_SECRET='<your-random-secret>'   # 与 NapCat 侧 OneBot token 保持一致
export DEEPSEEK_API_KEY='<your-api-key>'
# 可选：export TAVILY_API_KEY='<your-api-key>'
```

- `.env` 只放非密钥项（如 `NAPCAT_BASE_URL`，默认 `http://127.0.0.1:3000`）。
- 人设与安全文案可按需替换：`config/persona.md`、`config/persona.yaml`、`config/safety.yaml`、`config/onboarding.md`。

## 2. 路径 A：本地手动运行（开发 / 试跑）

1. 按 NapCat 官方说明在本机启动 NapCat 并扫码登录机器人 QQ 号。
2. 在 NapCat WebUI 的 OneBot 配置中：开启 HTTP API（默认本机 `3000` 端口）与 HTTP 上报，上报地址 `http://127.0.0.1:8090/onebot/event`，token 与 `ONEBOT_SECRET` 一致，消息格式选 **array**。
3. 启动 companion：

```sh
uv run uvicorn main:app --host 127.0.0.1 --port 8090
```

4. （可选）另开终端运行 NapCat 守护：

```sh
NAPCAT_BASE_URL=http://127.0.0.1:3000 NAPCAT_START_CMD='<your-napcat-start-command>' \
  uv run python -m companion.ops.watchdog
```

5. 验证：`curl http://127.0.0.1:8090/health/ready` 返回 `{"ready":true}`（注意：该端点仅在 `connection_health.enabled: true` 时注册，`config.example.yaml` 默认为 `false`，此时返回 404 属预期，开启连接健康检查后再用此端点验证）；用白名单账号私聊机器人应收到回复；再发一张图片验证视觉回复。

## 3. 路径 B：Docker Compose（仓库已带，未经生产验证）

暂存树根目录含 `Dockerfile` 与 `docker-compose.yml`，一条命令拉起 companion + NapCat 两个服务：

```sh
docker compose up -d
```

- companion 映射 `127.0.0.1:8090`，NapCat 映射 `127.0.0.1:3000`（HTTP API）与 `127.0.0.1:6099`（WebUI）；首次在 WebUI 扫码登录，登录数据保存在 `napcat-data` 卷。
- compose 中 NapCat 以 `reverse_http` 模式把事件上报到 `http://companion:8090/onebot/event`；`ONEBOT_SECRET` 仍须按 §1 经进程环境提供。
- 注意：镜像以非 root 用户（uid 10001）运行，compose 以只读方式挂载 `./config.yaml` 与 `./config`，`./data`、`./logs` 须对 uid 10001 可写。**此路径未在运维主机上验证过**（见 `deploy/README.md`），不等同于生产路径，遇到问题请以路径 A/C 为准排查。

## 4. 路径 C：systemd 生产部署（仓库 `deploy/` 资产）

生产路径使用仓库内 `deploy/` 资产（布局说明见 `deploy/README.md`，完整操作手册见 `docs/deployment-runbook.md`）：

```
deploy/
  systemd/   qqbot-companion.service.tmpl、qqbot-napcat.service.tmpl、
             qqbot-napcat.service.d/fingerprint.conf.tmpl
  runtime/   companion-launch.py（注入凭据后 exec uvicorn 于 127.0.0.1:8090）、
             napcat-watchdog.py、restore-service.sh、fingerprint-namespace.sh
  bin/       health-check.sh（只读探活）、sync-to-runtime.sh（默认 dry-run）
```

概要步骤（细节以 runbook 为准，手册含特定环境口径，套用时按占位替换）：

1. 设定部署根：`export QQBOT_BASE='<deploy-root>'`（其余变量 `QQBOT_RUNTIME`、`QQBOT_SYSTEMD_DIR` 等默认值与覆盖方式见 `deploy/README.md` 的变量表）。
2. 渲染 systemd 模板：把模板中的 `@BASE@` / `@RUNTIME@` 占位替换为实际路径（渲染命令示例见 `deploy/README.md`）。
3. 用 `deploy/runtime/restore-service.sh` 安装单元（root 执行；服务运行中、QQ 运行中或 3000/6099/8090 端口被占用时会拒绝；先用 `--check` 只校验不安装）。
4. `deploy/bin/health-check.sh` 做只读健康检查（单元、端口、HTTP 三类探针的 PASS/FAIL）。
5. 后续同步代码到运行目录用 `deploy/bin/sync-to-runtime.sh`（默认 dry-run，确认后加 `--apply`）。

> 指纹（fingerprint）数据与凭据文件**永不入仓**：仓库只含校验逻辑与脚本，实际数据文件由部署者在本机以受限权限（目录 0700 / 文件 0600）保管。

## 5. 测试与自检

```sh
scripts/test.sh           # uv run pytest -q --timeout=10，全程 fake OneBot、不连真实 QQ
uv run ruff check .
```

## 6. 安全提示

- 使用第三方协议端可能违反腾讯用户协议，自动化账号存在限制或封号风险，风险自负。
- 危机热线等安全文案默认适用于中国大陆；部署到其他地区时请替换为当地核实过的求助资源（`config/safety.yaml`）。
- 不要把任何真实密钥、QQ 号写入仓库文件；日志只应记录元数据，不记录消息正文与凭证。
