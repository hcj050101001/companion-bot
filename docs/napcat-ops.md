# NapCat / 本机链路运维（交接）

> **时效注记（2026-10-31，P7）**：本文成文于 W1 时期，其中
> `qq-relay.py`（:5700）中继链路**已废弃**——现行入站为 NapCat
> OneBot（:3000）直接上报 companion（:8090），代码中已无 relay。
> 本文涉及 relay 的小节仅作历史记录，启停/恢复/排障一律以
> [architecture.md](architecture.md) 与
> [deployment-runbook.md](deployment-runbook.md) 为准。

面向盒子上的真实运行时（非仅 Compose 示意）。Codex 续写或人工接手时，按本节启停与排障。**密钥与真实 QQ 号不要写入仓库**；下文一律占位符。

## 端口与进程

| 端口 | 进程 | 作用 |
|------|------|------|
| `127.0.0.1:3000` | NapCat / QQ（OneBot HTTP API） | `send_private_msg`、`get_login_info`、`get_status` 等 |
| `127.0.0.1:5700` | `$HOME/qq-relay.py` | 接收 NapCat 反向 HTTP 上报，验签后转发到 companion |
| `127.0.0.1:8090` | `uvicorn main:app`（companion-bot） | `/onebot/event` 入站处理与回复 |

链路：`NapCat(3000)` → 上报 `5700`（relay）→ `8090/onebot/event` → 再经 `3000` 发私聊。

## 启动 NapCat（必须带 `-q`）

唯一推荐入口：

```sh
bash $HOME/napcat-start.sh
```

脚本内容要点（仓外维护）：

- `pkill` 旧 QQ 进程后，带 `DISPLAY` 启动
- **必须**快速登录：`/opt/QQ/qq --no-sandbox -q <BOT_QQ>`
- 日志路径由脚本决定（常见 `/tmp/napcat.log`）

**禁止**直接起裸的 `/opt/QQ/qq`（不带 `-q`）。否则会掉扫码登录，`3000` 起不来。

验收：

```sh
curl -s -X POST http://127.0.0.1:3000/get_login_info \
  -H 'Content-Type: application/json' -d '{}'
# 期望 nickname 为「阿年」；user_id 为 <BOT_QQ>
```

账号展示名已改为 **阿年**（`get_stranger_info` 为准；`get_login_info` 偶发缓存旧昵称，以 stranger 为准）。

OneBot 配置（路径随安装变化，示例）：

`.../napcat/config/onebot11_<BOT_QQ>.json`

- `network.httpClients[0].url` = `http://127.0.0.1:5700/`
- `token` 与进程环境里的 `ONEBOT_SECRET` 一致；改 token 后需重启 NapCat 才生效

## qq-relay（仓外）

文件（**不进开源仓**）：

- 脚本：`$HOME/qq-relay.py`
- 环境：`$HOME/qq-relay.env`（含 `ONEBOT_SECRET`、`WEBHOOK_URL`、白名单等；已 gitignore 名 `qq-relay.env`）

当前约定：

- `WEBHOOK_URL=http://127.0.0.1:8090/onebot/event`
- 白名单示例：`WHITELIST_USER_IDS=<OWNER_QQ>`（仅本地 env，勿提交）

启动示例：

```sh
cd $HOME/companion-bot
set -a; source $HOME/qq-relay.env; set +a
python3 $HOME/qq-relay.py
```

要点：向 8090 转发时用同一 `ONEBOT_SECRET` **重签** `X-Signature`；日志只记元数据，不写消息原文。

## companion-bot（8090）

```sh
cd $HOME/companion-bot
# DEEPSEEK_API_KEY、ONEBOT_SECRET 必须在进程环境（禁止靠 .env 空键覆盖）
uv run uvicorn main:app --host 127.0.0.1 --port 8090
```

`ONEBOT_SECRET` / `DEEPSEEK_API_KEY` **禁止**从 `.env` 读入；`.env` 里不要留空的密钥键。

## 旧 webhook 例程（防双发）

切流后，Grok Bot 例程 **「QQ「年」实时回复」**（folder `qq-2`）必须保持 **暂停**。  
否则同一条私聊会既走 companion，又走旧 webhook，造成双发。

监控类例程「QQ「年」聊天监控」同样保持暂停。

## 守护（可选）

仓库内：`uv run python -m companion.ops.watchdog`  

`NAPCAT_START_CMD` 应指向 `bash $HOME/napcat-start.sh`（仍须带 `-q`），不要改成裸起 QQ。  
故障最多自动拉起一次；5 分钟未恢复只写 `logs/alerts.jsonl`（`napcat_down`），不再循环重启。

## WebUI-only 能力面（运维通道，不进运行时）

NapCat WebUI（127.0.0.1:6099）自带 5 类只经 WebUI 暴露的能力：
接口 Debug 调试、文件管理、凭据（token）管理、配置热重载、
进程/版本信息查询。方案 §6 N6 口径与收口文档
（`docs/napcat-integration-closeout.md` §4）定案：**这 5 类只作
运维通道，永不经 ActionGateway 运行时接入**——它们要么是调试面
（Debug 可绕开网关全部分级与审计）、要么与既有运维手段重叠
（重启归 systemd/看门狗、配置归文件 + 重启）。

使用约束：

- 访问只走本机回环 + WebUI 自身 token，不对外暴露端口；
- Debug 面如需实调某动作（例如为 `get_group_todo` 等挂起项取证），
  必须是**有人值守**的操作并把结论回填登记表/验证文档；无人值守
  的自动化不得调用 Debug 面；
- 经 Debug 面取得的任何凭证类返回值不落盘、不进日志与仓库。

## 图片理解的本地缓存取图（companion 主路径）

companion 的图片理解取图主路径经 ActionGateway `get_image` 读
NapCat/QQ 的本地缓存文件：缓存落在 QQ 数据目录
`napcat-v4/home/.config/QQ/nt_qq_<hash>/nt_data/Pic/<yyyy-MM>/Ori/`
下。companion 启动时把 `nt_qq_*/nt_data` 目录发现为本地根集合
（另可经 `media.vision.image_local_roots` 显式补充，见
`docs/config.md`），`get_image` 返回的路径必须落在根内且扩展名
为图片白名单才会被读取。发现只在启动时做一次：NapCat 换号登录
（`nt_qq_<hash>` 变化）或缓存目录迁移后，须重启 companion 才会
重新发现；启动日志出现 `code=no_roots` 告警即表示主路径未生效
（此时取图回落直连 CDN，在 fake-IP 代理环境下可能间歇超时）。

## 常见故障

| 现象 | 处理 |
|------|------|
| `3000` 不通、日志刷二维码 | 用 `napcat-start.sh` 重拉（确认 `-q`）；必要时扫码一次后再依赖快速登录 |
| 8090 全 401 | 三处 secret 是否一致：NapCat token / relay 环境 / uvicorn 进程环境；`.env` 是否有空 `ONEBOT_SECRET=` |
| 无回复但 3000 通 | 查 5700 relay 是否在跑、`WEBHOOK_URL` 是否指向 8090、旧 webhook 例程是否又被启用 |
| 双发 | 立刻暂停 `qq-2` 例程 |

## Compose 路径说明

`README.md` 里的 Docker Compose / `napcat-docker` 仍是通用部署示意。  
**当前盒子**以本节本机路径为准；两者密钥与端口约定相同，启动命令不同。
