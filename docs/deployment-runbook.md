# 发布规范 Runbook（部署 / 恢复 / 回滚 / 数据库 / P8 切换）

本文件是 QQBot（companion「阿年」+ NapCat）的运维发布规范，命令级、
可照做。适用两条线：现网多气泡线与框架线（P0–P8，方案见
`plans/2026-10-31-qqbot-framework-architecture.md`，不在本仓内、位于
部署根目录的 `plans/` 下）。架构现状见 [architecture.md](architecture.md)，
阶段状态见 [roadmap.md](roadmap.md)，记忆与投递的存储/迁移细节见
[memory-delivery.md](memory-delivery.md)，状态归属见
[state-ownership.md](state-ownership.md)，配置与密钥见
[config.md](config.md)。

## 名词与路径约定（全文统一）

| 记号 | 含义 |
|---|---|
| 部署根 | `~/agent-workspace/projects/QQBot-deploy/`（systemd 单元的 `WorkingDirectory`） |
| 生产代码 | 部署根下的 `repo/`（companion 进程从此目录以 `uvicorn main:app` 运行，监听 `127.0.0.1:8090`） |
| 框架 worktree | 部署根下的 `worktrees/framework-*`（各阶段独立 worktree + `ai/` 分支，现网不动） |
| 仓库内部署资产 | 本仓 `deploy/`（P7 入仓，布局见 §2.4） |
| 现行运行时副本 | 部署根下的 `runtime-24h/`（现网当前实际使用的守护/恢复/指纹资产，P8 切换时与 `deploy/` 统一，见 §2.4） |
| 两单元 | `qqbot-companion.service`、`qqbot-napcat.service` |
| 三端口 | NapCat HTTP `127.0.0.1:3000`、NapCat WebUI `127.0.0.1:6099`、companion `127.0.0.1:8090` |

纪律（全文适用）：

- **绝不动 NapCat**：常规发布与回滚只重启 `qqbot-companion`；NapCat
  进程、QQ 登录态、指纹资产不在发布动作范围内。
- **密钥永不进仓、永不入文档/日志正文**：密钥只在进程环境（启动器从
  仓外凭证文件注入）；本文档一律用占位符，不写真实账号/密钥。
- **先备份、后切换**：没有 §3.2 的备份目录与 MANIFEST，不得切版本。
- 本 runbook 的日常检查命令均为只读；写操作（恢复、重启、切换）只在
  对应章节的明确步骤里出现，执行前逐项确认所在章节。

---

## §1 日常健康检查

### 1.1 用法

在仓库根目录执行（P7 入仓脚本）：

```bash
bash deploy/bin/health-check.sh
```

该脚本是 §3.4 验证清单的只读封装：逐项检查并打印结果，退出码非 0
表示至少一项不达标。**它不修复任何东西**——不重启、不拉起、不改
配置；发现异常按 §2（单元丢失）或 §3.5（发布回滚）等对应章节处置，
不要指望重跑检查能自愈。

在 `deploy/` 尚未同步到位的环境（如 P8 统一前的现行副本环境），用
下面的等价只读命令逐项手查，口径与脚本一致：

```bash
systemctl is-active qqbot-companion.service qqbot-napcat.service
ss -ltnp | grep -E ':(3000|6099|8090)\b'
curl -s --noproxy '*' http://127.0.0.1:8090/health/ready
curl -s --noproxy '*' http://127.0.0.1:3000/get_status
```

### 1.2 逐项含义

| # | 检查项 | 达标口径 | 不达标说明什么 |
|---|---|---|---|
| 1 | `qqbot-companion.service` active | `active` | companion 未运行；若单元本身不存在（`not-found`），按 §2 恢复 |
| 2 | `qqbot-napcat.service` active | `active` | NapCat watchdog 未运行；同上注意区分「单元丢失」与「进程退出」 |
| 3 | 端口 3000 监听 | `127.0.0.1:3000` 有监听（进程为 QQ/NapCat） | NapCat HTTP 未起，companion 无处发送/探活 |
| 4 | 端口 6099 监听 | `127.0.0.1:6099` 有监听 | NapCat WebUI 未起，多为 NapCat 本体未运行 |
| 5 | 端口 8090 监听 | `127.0.0.1:8090` 有监听（companion） | companion 未起或启动即失败，先看其 journal |
| 6 | `GET 127.0.0.1:8090/health/ready` | 返回 `{"ready":true}` | 事件消费者未就绪；注意 ready 只证明消费者在跑，不证明 NapCat 登录与真实投递（见 [connection-health.md](connection-health.md)） |
| 7 | `GET 127.0.0.1:3000/get_status` 可达 | 脚本口径：端点可达即 PASS，`online/good` 字段只作 best-effort 报告、不下结论 | 人工判读时才看字段：`online=false` 多为 QQ 登录态失效（按 §2.5 处理）；`good` 是 NapCat 侧字段，其硬编码盲区注记见方案 §3.6，不得只凭 `good=true` 宣称链路全通。发布验证（§3.4 第 5 项）要求 `online=true`、`good=true`，严于本脚本 |

### 1.3 只读原则

- 日常巡检只许 `is-active` / `cat` / `ss` / `curl` 上述端点等只读
  命令；不得在巡检名义下 `restart` / `enable` / 改单元文件。
- 健康检查不发真实 QQ 消息、不调用任何发送接口；真实链路验证只在
  §3.4 的隔离验证与 §5 的真机冒烟里、按各自口径执行。
- 检查结果异常时先留痕（时间、哪项不达标、原始输出摘要），再处置；
  不得把「检查脚本退出码非 0」直接等同于某个根因。

---

## §2 换机 / 单元丢失恢复

背景：平台换机或系统重置会丢失 `/etc/systemd/system/` 下的单元注册
（已多次发生：两单元 inactive、端口无监听，而仓与数据完好）。恢复的
唯一路径是 restore 脚本；不要手工拼单元文件。

### 2.1 现象识别

以下组合出现，即按「单元丢失」处理，而不是按应用故障排查：

- `systemctl is-active qqbot-companion.service qqbot-napcat.service`
  返回 `inactive`，或单元为 `not-found` / `/etc/systemd/system/` 下
  找不到 `qqbot-*.service`；
- 三端口（3000/6099/8090）均无监听；
- 部署根、`repo/`、`runtime-24h/`（或仓库 `deploy/`）文件仍在。

若单元在、仅 companion 反复重启，先看 journal 按应用故障排查，
不要跑 restore（restore 在服务 active 时会拒绝，见 §2.3）。

### 2.2 恢复步骤（命令级）

**P8 之前，现网恢复用部署根下的现行副本**（`deploy/` 尚未同步到
现网，生产 `repo/` 里没有 `deploy/` 目录，不要在现网 repo 根目录
找它）：

```bash
cd ~/agent-workspace/projects/QQBot-deploy

# 1) 只校验、不落任何变更：指纹输入、账号格式、namespace 能力
sudo bash runtime-24h/restore-service.sh --check

# 2) 校验通过后，以 root 执行恢复（安装/恢复单元、daemon-reload、enable --now 两单元）
sudo bash runtime-24h/restore-service.sh
```

**P8 之后（或在含 `deploy/` 的框架工作树里操作时）**用仓库版本，
语义与现行副本对齐；换机到全新机器/新路径时，先把仓库资产同步到
目标机的 runtime 目录，再恢复：

```bash
cd <含 deploy/ 的工作树>
bash deploy/bin/sync-to-runtime.sh --apply   # 先 dry-run 核对再 --apply
sudo bash deploy/runtime/restore-service.sh --check
sudo bash deploy/runtime/restore-service.sh

# 3) 按 §1 复验（任一版本恢复后都执行）
bash deploy/bin/health-check.sh
```

说明：

- restore 支持以环境变量覆盖路径：`QQBOT_BASE`、`QQBOT_RUNTIME`、
  `QQBOT_SYSTEMD_DIR`、`QQBOT_FINGERPRINT_DIR`（脚本另支持
  `QQBOT_CREDENTIALS_FILE` 覆盖凭证文件路径）；默认值为现行部署
  布局。换机到新路径时用覆盖变量指向目标位置，不要改脚本正文。
  注意 **`sudo` 默认会丢弃环境变量**：覆盖变量必须写在 sudo 之后、
  以赋值形式传入，例如
  `sudo QQBOT_BASE=/new/base bash deploy/runtime/restore-service.sh`；
  脚本启动时会回显实际生效的 base/runtime/systemd_dir/fingerprint/
  credentials 路径（不含凭证内容），先核对回显再继续。
- **`QQBOT_SYSTEMD_DIR` 不是隔离手段**：脚本只在它等于真实
  `/etc/systemd/system` 时才执行 `daemon-reload` / `enable --now`；
  指向其他目录时只安装文件并明确打印「未激活」。真正的启动演练
  必须在独立 VM/容器里做（方案 §8 口径，本机结论见 §2.6）。
- restore 的单元来源是单一模式：同树三份模板齐备走模板渲染，
  否则整套用 runtime 已渲染文件并断言其引用当前 `QQBOT_BASE`
  （混装/旧路径单元会被拒绝）；单元实际执行的
  `companion-launch.py`、`napcat-watchdog.py`、
  `fingerprint-namespace.sh` 若不在 runtime 目录，restore 会先从
  脚本同目录安装它们，`--check` 对缺失同样报错。
- `--check` 只校验：只读检查指纹输入（目录/文件权限与格式）、保存
  账号格式与 namespace 能力，不写 `/etc`、不启服务。
- 恢复本体只替换设备指纹文件、安装两单元并 `enable --now`；不触碰
  QQ 账号与会话数据、不触碰生产 `config.yaml` 与 `memory.db`。
- /etc 单元在 P8 后是渲染实体文件：**单元内容变更不能靠 sync 生效**
  （sync 只写 runtime 目录）。变更单元须在停服窗口重跑 restore，
  或手工 `install` 渲染结果到 /etc 后 `daemon-reload` 并按 §1 复验，
  变更内容记入切换记录。

### 2.3 拒绝条件（脚本的既定行为，不是故障）

- 任一受管服务处于 active 时，restore **拒绝运行**（防止在运行中
  替换指纹/单元）。确需恢复时，先按正常运维停服窗口处理，再执行。
- 任一服务端口（3000/6099/8090）已被占用、或 QQ 进程在跑时同样
  拒绝。先查明占用者，不要强杀不明进程。
- 非 root 执行直接拒绝；`--check` 同样需要 root（namespace 能力
  校验所需）。

### 2.4 runtime-24h 现行副本与仓库 deploy/ 的同步关系

- 现网当前运行的是部署根下 `runtime-24h/` 的副本（单元模板渲染
  目标、`companion-launch.py`、`napcat-watchdog.py`、
  `fingerprint-namespace.sh`、`restore-service.sh` 均以该目录为准，
  systemd 单元的 `ExecStart` 指向该目录）。
- P7 起，同一套资产入仓为本仓 `deploy/`，布局为：

  ```text
  deploy/systemd/*.tmpl                  # @BASE@ 占位，渲染后安装
  deploy/systemd/qqbot-napcat.service.d/fingerprint.conf.tmpl
  deploy/runtime/restore-service.sh
  deploy/runtime/companion-launch.py
  deploy/runtime/napcat-watchdog.py
  deploy/runtime/fingerprint-namespace.sh
  deploy/bin/health-check.sh             # 只读
  deploy/bin/sync-to-runtime.sh
  ```

- 同步方向：以仓库 `deploy/` 为源，用 `deploy/bin/sync-to-runtime.sh`
  把渲染/同步结果落到 `runtime-24h/` 现行副本；**P8 切换时统一**
  （见 §5）。该脚本**默认 dry-run**（只打印将写内容与 diff），
  加 `--apply` 才落盘；落盘时对内容有差异的既有文件先在旁备份
  为 `<name>.pre-deploy-sync-<UTC 时间戳>`；指纹目录、凭证文件与
  日志永不在同步名单内。P8 之前，现网继续用现行副本，不要在
  日常发布里顺手改 `runtime-24h/` 或单独同步某一文件造成两处
  漂移。
- 任一处改动（仓内或现行副本）必须在同一次变更里说明另一处的
  状态；发现两处不一致时，以「现网正在运行的副本」为现网事实、
  以仓库版本为待切换目标，差异记入切换记录（§5.6 模板）。

### 2.5 QQ 登录态说明

- NapCat 由 watchdog 在三条件齐备（QQ 未运行、3000/6099 未占用、
  锁可用）时以**保存账号 `-q` 拉起**，正常情况下恢复后免扫码、
  无需用户介入。恢复完成后以 §1 第 7 项（`get_status`
  `online=true`）为准确认登录态。
- 只有登录态**真正过期**（watchdog 已拉起进程但 `online` 持续为
  false、或 NapCat 明确要求重新登录）才需要用户扫码：这是**待
  用户事项**，攒入待办并明确告知，不要反复重启碰运气、不要声称
  「已恢复」。
- 设备指纹固定接线已完成但实效未验证（方案 §3.9）：恢复成功
  不等于指纹生效，不得在记录中宣称指纹已验证。

### 2.6 演练记录（P7）

**结论：未验证（无可用隔离环境）。** 2026-10-31 由 P7 协调者实测
本机隔离条件，证据如下（完整记录在部署目录
`~/agent-workspace/projects/QQBot-deploy/drill/drill-record.md`）：

- 当前进程能力集仅 `cap_net_admin, cap_net_raw, cap_sys_ptrace`，
  无 CAP_SYS_ADMIN / CAP_MKNOD（`mknod` 实测 Operation not
  permitted）；`debootstrap` 在解包 base-files 阶段因 tar 失败中止。
- unprivileged user namespace 可创建、tmpfs 可挂载，但 `mount -t
  proc`、sysfs 在任意位置均 EPERM（宿主外层容器运行时的 /proc、/sys
  带锁定掩码挂载，内核禁止子 userns 新建实例）；
  `systemd-nspawn`（直接与 userns 包裹两种方式）均在挂载 sysfs 一步
  失败。Docker 命令不存在。
- 因此 systemd 无法在本机的任何隔离环境内作为 PID 1 运行，
  restore 演练（装单元 → 起服务 → ready → 摘除）**无法在本机诚实
  执行**。按方案 §8 P7 口径记未验证；§2 的步骤是标准步骤，不构成
  「已演练/已验证」声明，不得以换目录演练充数。真正的 restore
  演练须在具备 CAP_SYS_ADMIN 的一次性 VM 上按 §2 执行并回填本节。

已做的替代核对（仅如实分列，不冒充演练）：

- `deploy/bin/health-check.sh` 对现网只读运行：7 项全 PASS
  （两单元 active、三端口监听、ready=true、get_status 含
  online/good 字段），退出码 0。
- 仓库版 `deploy/runtime/restore-service.sh --check` 对现网只读
  运行通过（指纹输入/账号格式/namespace 能力校验 OK、无任何
  更改），退出码 0。
- `tests/test_deploy_assets.py` 断言三份模板渲染结果与仓库内
  golden 单元（`tests/fixtures/deploy/`，2026-10-31 自现行单元
  快照）逐行等价——该断言无条件执行、不依赖仓外文件；对
  `runtime-24h/` 与 /etc 现行文件的比对保留为本地可选检查。
  Dockerfile/脚本静态门禁全过（资产一致性，非演练）。
- 回滚演练（切上一 tag + 恢复配置与 DB 快照）：现网部分同因无
  隔离环境**未演练**；DB 快照回滚的测试库演练记录见
  [memory-delivery.md](memory-delivery.md)「回滚演练记录（P5，测试库）」。

---

## §3 常规发布流程（框架线与现网多气泡线通用）

### 3.1 开发与审查

1. 在独立 worktree + `ai/<task-name>` 分支开发，不直接改
   `main`；不碰生产 `config.yaml`、不重启现网服务、不发真实 QQ
   消息（测试一律 FakeAdapter/Fake LLM）。
2. 阶段完成必须过 Codex Review（强制），调用口径固定为：

   ```bash
   codex exec -m gpt-6.1-sol -c model_reasoning_effort="high"
   ```

   Review 针对当前分支相对基线的 diff；findings 经主执行者逐条
   核查后只修真实有效的问题，修完重测，必要时再送一轮。
3. 全套测试通过才进入部署准备：

   ```bash
   bash scripts/test.sh
   ```

### 3.2 部署前备份（硬前置）

在部署根下新建备份目录（命名沿用现行惯例）：

```bash
cd ~/agent-workspace/projects/QQBot-deploy
BACKUP="companion-<主题>-backup-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
mkdir -m 700 "$BACKUP"
cp repo/config.yaml "$BACKUP/config.yaml"
chmod 600 "$BACKUP/config.yaml"
```

备份目录权限 `700`，内含：

- 生产 `config.yaml` 副本（权限 `600`）；
- 若本次发布涉及 schema/记忆变更，另按 §4 规程做 `memory.db`
  一致性快照并放入备份目录（或在 MANIFEST 中写明快照路径）；
- `MANIFEST` 文件，至少记录以下各行（**磁盘版本与运行版本分开
  记**，两者不一致本身就是要留痕的事实）：

  ```text
  date: <YYYY-MM-DD HH:MM:SS +0800>
  topic: <主题>
  disk_head: <repo/ 磁盘上的 git HEAD，即将切换前>
  target_commit: <本次要部署的提交>
  running_version: <运行进程对应的版本/提交，未知则写 unknown 并注明>
  companion_pid: <发布前 systemctl MainPID>
  napcat_pid: <发布前 NapCat(QQ) 进程 PID，回滚/复验时比对不变>
  config_sha256: <发布前生产 config.yaml 的 sha256>
  config_sha256_baseline: c66b8d90…c96ca9
  switches: <逐项写明，至少含 read_only_tools 与各只读工具、
    connection_health、proactive_care 的 enabled 值>
  db_snapshot: <memory.db 快照路径或 none（无 schema/记忆变更时）>
  ```

  注：`config_sha256_baseline` 是 2026-10-31 核定的生产配置基线
  指纹（截断表示）；发布时 MANIFEST 记的实际 `config_sha256` 与
  基线不一致时，必须在切换记录里说明差异来源（哪次经批准的配置
  变更），不得默默以新值覆盖基线。

### 3.3 切换

```bash
cd ~/agent-workspace/projects/QQBot-deploy/repo
git status --porcelain        # 必须无输出：工作区干净
git checkout <目标提交>        # 或 git switch 到目标 tag/分支后确认 HEAD
git rev-parse HEAD             # 与 MANIFEST 的 target_commit 一致
sudo systemctl restart qqbot-companion.service   # 只重启 companion
```

- 只切代码不改配置：生产 `config.yaml` 不在 git 切换范围内；若
  本次发布确需改配置，改动先写入备份流程留痕，再单独说明。
- **绝不动 NapCat**：不 restart/stop `qqbot-napcat.service`，不
  触碰 QQ 进程与登录态。

### 3.4 验证清单（逐项打勾，任一关键项失败按 §3.5 回滚）

| # | 项目 | 命令/方法 | 达标口径 |
|---|---|---|---|
| 1 | 两单元 active | `systemctl is-active qqbot-companion.service qqbot-napcat.service` | 均为 `active` |
| 2 | 三端口监听 | `ss -ltnp \| grep -E ':(3000\|6099\|8090)\b'` | 3000/6099/8090 均有监听 |
| 3 | companion ready | `curl -s --noproxy '*' http://127.0.0.1:8090/health/ready` | `{"ready":true}` |
| 4 | NapCat 未被动过 | `systemctl show -p MainPID qqbot-napcat.service` 与 QQ 进程 PID（`pgrep -f 'opt/QQ/qq'`）比对 MANIFEST 的 `napcat_pid`；或直接跑 `deploy/bin/health-check.sh` 看其单元/端口项 | PID 不变 |
| 5 | OneBot 状态 | `curl -s --noproxy '*' http://127.0.0.1:3000/get_status` | `online=true`、`good=true` |
| 6 | 全套测试 | 在目标提交上 `bash scripts/test.sh`（本项应在部署前完成；发布后复验以 1–5、7、8 为准，避免为跑测试延误回滚） | 全过（xfail 只许是登记册内既有项） |
| 7 | 隔离验证用例 | `bash scripts/test.sh` 中的 FakeAdapter/对话剧本用例即此项的执行入口（普通回复、危机固定话术且零工具调用、忘掉、工具直出/合成、多气泡条数与间隔） | 逐项与剧本预期一致；隔离验证不能替代真机验证（真机口径见 §5） |
| 8 | 开关状态 | 对照 MANIFEST 的 `switches` 与运行配置 | 一致，尤其 `proactive_care.enabled` 不得被发布悄悄打开 |

### 3.5 回滚（任一关键项失败立即执行，不先「再观察一下」）

```bash
cd ~/agent-workspace/projects/QQBot-deploy
BACKUP="<§3.2 建立的备份目录名>"              # 本块自带定义，不依赖其他代码块
git -C repo checkout <上一版本提交>            # MANIFEST/上次记录中的版本
cp "$BACKUP/config.yaml" repo/config.yaml      # 恢复备份 config
sha256sum repo/config.yaml                     # 须与 MANIFEST 记录的备份 sha256 一致
sudo systemctl restart qqbot-companion.service # 仍只重启 companion
```

回滚后用 §3.4 同一份清单复验，逐项达标才算回滚完成。若本次
发布动过数据库 schema，先按 §4.3 回滚数据库，再切代码。

### 3.6 发布记录

每次发布至少留一行记录（时间、主题、目标提交、备份目录、验证
清单结果、是否回滚），格式可沿用 §5.6 模板的简版。没有记录的
发布视为未完成。

---

## §4 数据库规程（衔接方案 §9）

对象：`memory.db`（路径由配置 `memory.sqlite_path` / 正式树
`sqlite_path` 决定，默认 `data/memory.db`），含短期历史、投递
台账、入站 claim 与 `schema_meta.schema_version`（当前 1）。
存储拆分与一致性契约见 [memory-delivery.md](memory-delivery.md)。

### 4.1 一致性备份（唯一认可的做法）

**禁止对运行中的库直接文件拷贝**——WAL 模式下会丢已提交内容。
一致性备份四步，缺一不可：

1. 停 companion 接入（停 `qqbot-companion` 或置维护开关），确保
   无写入方；
2. 执行 WAL checkpoint；
3. 用 SQLite backup API 复制，或在停机后拷贝 **db + wal + shm
   三件套**；
4. 对备份执行 `PRAGMA integrity_check`，结果为 `ok` 才算备份
   完成。

示意（停机后、路径按实际配置调整）：

```bash
cd ~/agent-workspace/projects/QQBot-deploy/repo   # data/memory.db 相对此目录
sudo systemctl stop qqbot-companion.service
sqlite3 data/memory.db "PRAGMA wal_checkpoint(TRUNCATE);"
sqlite3 data/memory.db ".backup '<备份目录>/memory.db'"
sqlite3 '<备份目录>/memory.db' "PRAGMA integrity_check;"
```

### 4.2 schema 迁移纪律

- 迁移**只在启动期、接入开启前**执行（现行实现：`Database`
  初始化时识别旧 schema 并 additive 迁移，见
  [memory-delivery.md](memory-delivery.md) 的 v0 → v1 记录）。
- 不得在服务对外处理消息期间手工改表、跑迁移脚本或替换库文件。
- 迁移类发布在 §3.2 备份时必须包含迁移前一致性快照（§4.1），
  MANIFEST 的 `db_snapshot` 不得为 `none`。

### 4.3 回滚

回滚 = 切回旧 tag（代码）+ 恢复迁移前快照（数据库），顺序是
先停 companion → 恢复快照 → 切代码 → 启动 → 按 §3.4 复验。
恢复快照时必须处理 WAL/SHM，缺一不可：

1. 停 companion，确认无写入方；
2. 把当前 `memory.db`、`memory.db-wal`、`memory.db-shm` **整体
   移走**（不要只覆盖主库文件——残留的旧 WAL 会被 SQLite 回放，
   可能把库带回快照之后的事务、甚至损坏）；
3. 放入快照 `memory.db`，确认其旁**没有**旧的 wal/shm；
4. 对恢复后的库执行 `PRAGMA integrity_check`，结果为 `ok` 才许
   启动。

### 4.4 损失面（如实写明，不得淡化）

**不存在零损失回滚。** 以迁移前快照回滚后：

- 迁移后新收的消息记录**丢失**（含已发送回复对应的历史投影）；
- 迁移后执行的「忘掉」删除**被撤销、已删数据可能复现**；
- 台账/claim 同理回到快照时点，快照后已接受的投递状态不再存在；
- 同库的用户状态一并回退：`user_state`（含首次告知/onboarding
  标记）、`pack_preferences`（风格包选择）回到快照时点——用户
  可能被重新首次告知、风格偏好被重置，这是快照回滚的预期结果，
  不是故障。

P5 已在测试库按本规程完成回滚演练并实测核对了该损失面（升级 →
收消息/执行忘掉 → 回滚到旧代码 + 迁移前快照，旧代码可正常打开
库、损失面与记录一致），结论见 [memory-delivery.md](memory-delivery.md)
「回滚演练记录（P5，测试库）」。每次现网切换记录必须逐字确认本
节损失面（§5.6 模板含确认栏），不能只写「已备份」。

---

## §5 框架线切现网检查单（P8 专用）

本节只用于框架线（P0–P8 成果）切换现网这一次操作，与 §3 的常规
发布分开执行、单独开部署窗口，不与任何阶段开发混做（方案 §9）。

### 5.1 前置条件（全满足才许切换）

- [ ] P0–P8 全部收口（P8 为框架侧收口），各阶段 Codex Review 完成；
- [ ] Codex 补审清零：共七笔因额度降级留下的待补审项——P4
      （`f03fc60..1af8a22`）、P5（`f03fc60..2343c9a`）、P4×P5
      合并（`f03fc60..b2c2bdd`）、P6（`b2c2bdd..9cb3a7d`）、P7
      （`b2c2bdd..9fcdad3`）、P6×P7 合并（`b2c2bdd..60f6d51`）、
      P8（`60f6d51..` P8 收口提交）（见 CHANGELOG [0.9.0] 段与
      [accepted-residuals.md](accepted-residuals.md) R1），无未裁
      决的高严重度问题；
- [ ] P0 已知差异登记册（`tests/KNOWN_DIFFERENCES_P0.md`）逐条
      处置完毕：未处置差异不得带入 P8；登记册 15 条已全部关闭
      （最后一条文档级 KD-S03 已在 P6 关闭）；
- [ ] §2.6 的 restore 隔离演练已完成并回填（或明确记为未验证，
      并据此决定是否推迟切换）；
- [ ] 全套测试在目标 tag 上通过：`bash scripts/test.sh`；
- [ ] **真机冒烟留痕**（见 §5.4）：有成功记录才许打 `v1.0.0`；
      无记录则只打 `v0.9.x`，不得以隔离/夹具测试通过充数。

### 5.2 备份三件套（切换前，缺一不切）

按 §3.2 + §4.1 执行，备份目录权限 `700`：

1. **代码**：记录现网当前提交（磁盘 HEAD 与运行版本分开记入
   MANIFEST），该提交即回滚目标；
2. **配置**：生产 `config.yaml` 副本（`600`）+ MANIFEST（含
   config sha256 与基线 `c66b8d90…c96ca9` 对照、开关状态、
   companion/NapCat PID）；
3. **数据库**：`memory.db` 迁移前一致性快照（§4.1 四步 +
   `integrity_check` 为 `ok`），路径记入 MANIFEST。

### 5.3 切换步骤

```bash
cd ~/agent-workspace/projects/QQBot-deploy/repo
git status --porcelain                 # 必须干净
git checkout <框架线目标 tag>           # v1.0.0 或 v0.9.x，见 §5.1
sudo systemctl restart qqbot-companion.service   # 只重启 companion，绝不动 NapCat
bash deploy/bin/health-check.sh        # 或按 §1.2 逐项手查
```

同时在此窗口完成 `deploy/` 与 `runtime-24h/` 现行副本的统一
（§2.4）：以仓库 `deploy/` 为源经 `sync-to-runtime.sh` 同步，
同步结果与差异记入切换记录；此后只维护仓库版本一处。注意顺序：
sync 在 restart **之后**只改磁盘文件，正在运行的进程仍用旧的
launcher/watchdog；若本次同步改动了运行时脚本且要求立即生效，
须在记录中注明并再重启一次 companion（仍绝不动 NapCat），或
明确留到下个窗口。

### 5.4 冒烟项（真实链路，逐项留痕）

由用户或值守在真实 QQ 私聊链路上执行并留成功记录（时间、类型、
结果；不留聊天原文）：

- [ ] 私聊普通消息一条：正常回复（含多气泡形态符合预期）；
- [ ] 私聊危机消息一条：固定危机话术、零工具调用。**注意本项会
      走真实安全告警通道**（诊断告警与后果性通知，含可能启用的
      `ALERT_QQ`/邮件告警）：执行前先确认告警接收人与临时静默
      策略、避免被当作真实危机事件，并在冒烟记录中注明告警去向；
- [ ] 私聊工具消息一条：只读工具（如天气/时间/搜索）按契约
      直出或合成；
- [ ] 多气泡一条：条数/间隔符合现行拆分与节奏契约。

四项全过才算冒烟成功；任一失败按 §5.5 回滚。冒烟记录是打
`v1.0.0` 的前置证据（§5.1），后补无效。

### 5.5 回滚判据与步骤

判据（任一成立即回滚，不设「观察期」）：

- §3.4 验证清单任一关键项（1–5、8）失败且 15 分钟内不能定位
  为与切换无关的外部原因；
- §5.4 冒烟任一项失败；
- 出现重复发送、危机链路异常、忘掉失效等 §3 不变量（方案）
  相关症状。

步骤：按 §4.3 先恢复 DB 快照（若本次动过 schema）→ 切回 §5.2
记录的上一 tag → 恢复备份 config → 只重启 companion → 按 §3.4
清单复验 → 在切换记录中写明回滚原因与 §4.4 损失面确认。

### 5.6 切换记录模板

```text
框架线切现网记录
version: <目标 tag，如 v1.0.0 / v0.9.x>
from_commit: <切换前现网提交>
date: <YYYY-MM-DD HH:MM:SS +0800>
operator: <执行人/值守>
backup_dir: <备份目录名>
config_sha256: <实际值>（基线 c66b8d90…c96ca9，差异说明：<无/说明>）
db_snapshot: <快照路径 + integrity_check 结果>
preconditions: P0–P8 收口 <是/否>；Codex 补审清零 <是/否>；
  已知差异清零 <是/否>；restore 演练 <已回填/未验证>
smoke: 普通 <通过/失败>；危机 <通过/失败>；工具 <通过/失败>；
  多气泡 <通过/失败>；留痕位置 <路径/链接>
deploy_sync: deploy/ → runtime-24h 已统一 <是/否，差异：>
result: <切换成功 / 已回滚（原因：）>
loss_surface_confirmed: [ ] 已确认 §4.4 损失面：快照回滚将丢失
  切换后新收消息记录，已执行的「忘掉」删除可能被撤销、已删
  数据可能复现，用户状态/风格偏好/onboarding 标记一并回退。
  不存在零损失回滚。（确认人：____）
```

---

## 附录 A §5.10 工具诊断统一观测出口裁决记录

- **事项**：方案 §5.10 要求把工具诊断字段（`error_category` /
  `http_status` / 计数等）上升为统一观测出口（经 pipeline
  observer 统一汇出，或扩展 `/metrics` / ready 端点）。
- **裁决（P7）**：**不在 P7 落地，留 P8 收口一并做。**
- **理由**：
  1. 方案 §8 的 P7 产出清单只有 deploy 入仓、Dockerfile、文档
     重写、examples，不含 §5.10 实现；在部署资产阶段夹带代码
     变更违反阶段边界。
  2. 现状（P4 后）诊断保留在 provider 客户端：工具循环把结构化
     诊断留在 `CompanionBrain.last_tool_diagnostics`（见
     `companion/brain/__init__.py`），尚未经统一出口汇出；该
     现状不丢数据，只是未统一。
  3. 统一出口涉及 pipeline observer 接口变更（runner 的观察者
     签名与汇出形态），属于框架收口级改动，应随 P8 全量回归
     一并验证，而不是在 P7 单独引入。
- **P8 具体待办**：
  1. 工具诊断经 pipeline observer 统一出口汇出，字段口径按
     §5.10（`trace_id/stage/event_type/result_code/elapsed_ms`
     + 工具诊断字段），正文永不入日志；
  2. `/metrics` 文本端点或 ready 端点扩展按 §5.10 落地：各
     Stage 时延、工具成功率、模型 token/估算成本（按用户哈希
     聚合）、危机命中计数（只计数不存内容）、发送额度余量；
  3. 完成后同步更新 [architecture.md](architecture.md) 的
     可观测性一节与本附录状态。
- **P8 落地状态（2026-10-31，已完成）**：待办 1 已落地、待办 2
  除「估算成本」外已落地（成本无价格数据源、未实现，见下口径
  裁决）——
  `companion/pipeline/observers.py`（`StageMetrics` +
  `MetricsObserver` + `DiagnosticsLogObserver`，只依赖标准库）、
  Respond Stage 收割 `last_tool_diagnostics` 进
  `EventContext.tool_diagnostics`、`/metrics` 文本端点恒注册
  （仅回环可达为部署口径）、`SendQuota.snapshot()` 只读余量；
  待办 3 已同步（architecture 可观测性一节已改写）。**口径裁决
  （P8）**：统一出口覆盖工具循环诊断，`result_code` 为统一结果
  字段；web_search 自身的 `error_category`/`http_status` 结构化
  字段保留在其工具层日志、不并入 observer 出口；token 指标
  口径仅限工具循环 respond 步（指标名带 `tool_loop` 前缀，
  普通生成与 judge 的模型调用不在内）；「估算成本」无价格
  数据源、未实现，只出 token 计数。以上口径与残留登记见
  [accepted-residuals.md](accepted-residuals.md) R23–R25。

## 附录 B 文档与代码一致性自查清单（2026-10-31）

核对日期：**2026-10-31**。方法：对本文与
[architecture.md](architecture.md) 中出现的每个文件路径、命令、
端口、服务名，用 `ls` / `grep` 在本 worktree、现行
`runtime-24h/` 与现网只读命令（`systemctl is-active/cat`、
`ss`、`curl` 只读端点）中逐项核对；不一致时以代码/现网事实
为准改文档，发现逐条列于表后。

| # | 核对项 | 结果 | 依据 |
|---|---|---|---|
| 1 | 服务名 `qqbot-companion.service` / `qqbot-napcat.service` | ☑ 一致 | 现网 `systemctl is-active` 均 `active`；`runtime-24h/` 同名单元文件存在 |
| 2 | 端口 3000 / 6099 / 8090 | ☑ 一致 | 现网 `ss -ltnp` 三端口均监听（3000/6099 为 QQ 进程、8090 为 companion）；`docker-compose.yml` 同口径 |
| 3 | `GET /health/ready` 返回 `{"ready":true}` | ☑ 一致 | `companion/ingress/__init__.py` 有该路由；现网 `curl` 实测 `{"ready":true}` |
| 4 | `GET :3000/get_status` 的 `online/good` | ☑ 一致 | `companion/adapters/onebot.py` `health()` 解析该端点；现网实测 `online=true, good=true` |
| 5 | `POST /onebot/event` 入站路径 | ☑ 一致 | `companion/ingress/__init__.py` 路由存在 |
| 6 | 入口 `main.py` → `companion.ingress.create_app`，uvicorn 8090 | ☑ 一致 | `main.py` 与现网进程命令行（`uvicorn main:app --host 127.0.0.1 --port 8090`） |
| 7 | pipeline 11 个 Stage 名与顺序 | ☑ 一致 | `companion/pipeline/runner.py` 的 `STAGE_ORDER` 与 `companion/pipeline/stages.py` 的 `STAGE_CLASSES` 逐名核对 |
| 8 | adapters：`base.py` / `onebot.py` / `fake.py` | ☑ 一致 | `ls companion/adapters/`；`PlatformAdapter`/`Ignore`/`SendReceipt` 均在 `base.py` |
| 9 | memory 三存储：`conversation.py` / `ledger.py` / `claims.py` + `database.py` | ☑ 一致 | `ls companion/memory/`；`docs/state-ownership.md` 与 `docs/memory-delivery.md` 同口径 |
| 10 | config Pydantic 树 `AppConfig`（`companion/config.py`） | ☑ 一致 | `grep '^class .*Config' companion/config.py`；`docs/config.md` 同口径 |
| 11 | tools 插件：`tools/plugins.py`、`tools/read_only.py`、三只读工具文件 | ☑ 一致 | `ls companion/tools/`（`current_time.py` / `weather.py` / `web_search.py`） |
| 12 | `examples/dummy_tool_plugin.py` | ☑ 一致 | `ls examples/` |
| 13 | `scripts/test.sh`（全套测试入口） | ☑ 一致 | 文件存在，内容为 `uv run pytest -q --timeout=10` |
| 14 | 生产 config sha256 基线 `c66b8d90…c96ca9` | ☑ 一致 | 2026-10-31 对生产 `repo/config.yaml` 只读 `sha256sum`，首尾与基线截断值吻合 |
| 15 | 现网开关基线（只读工具开、连接健康开、主动关怀关） | ☑ 一致 | 2026-10-31 只读解析生产 `config.yaml` 的 `enabled` 字段（未读取/记录任何密钥） |
| 16 | `runtime-24h/restore-service.sh` 现行副本及其拒绝/active 与 `--check` 语义 | ☑ 一致 | 逐行读现行脚本：active 时拒绝、`--check` 只校验、root 要求、端口占用拒绝均与 §2 口径一致 |
| 17 | watchdog 以保存账号 `-q` 拉起 QQ | ☑ 一致 | `napcat-control.sh` 的 `run` 路径读保存账号并以 `-q <account>` 启动（本文不写账号值） |
| 18 | 仓库 `deploy/` 布局（§2.4 所列路径） | ☑ 一致（复核后） | 初核时并行任务尚在落地，后于同日复核：`deploy/systemd/{qqbot-companion.service.tmpl, qqbot-napcat.service.tmpl, qqbot-napcat.service.d/fingerprint.conf.tmpl}`、`deploy/runtime/{restore-service.sh, companion-launch.py, napcat-watchdog.py, fingerprint-namespace.sh}`、`deploy/bin/{health-check.sh, sync-to-runtime.sh}` 已全部在本 worktree 出现，并逐个读取核对语义（restore 的覆盖变量/active 拒绝/`--check`、health-check 的只读与 get_status 可达口径、sync 的默认 dry-run/`--apply` 与排除规则均与本文一致） |
| 19 | relay `:5700` 链路 | ☑ 已按代码改文档 | `grep 5700 companion/` 无结果（仅旧文档提及）；[architecture.md](architecture.md) 已删除该链路并注明，见发现 D1 |
| 20 | Dockerfile / docker-compose.yml，EXPOSE 8090 | ☑ 一致 | 仓库根目录两文件存在；Dockerfile（P7 并行任务产出）含 `HEALTHCHECK`（探 `/health/ready`）与非 root `USER appuser`，与 P7 产出清单一致 |

发现的不一致（均已以代码为准改文档，详见本次汇报）：

- D1：旧 architecture 的 relay `:5700` 转发链在 `companion/`
  代码中不存在（入站由 FastAPI 直接接收 OneBot 上报）；仓外
  `docs/napcat-ops.md` 与已移除的内部交接文档（原 `docs/codex-handoff.md`，开源清洗时删除）仍按旧盒子
  链路描述 relay，**不在本次修改范围**，已在 architecture 中
  注明分工，避免两处口径互相冒充。
- D2：方案 §6 的目标布局与实际包结构不完全一致（见
  architecture「目标布局与实际的差异」一节）：无独立
  `core/`、`ports/`、`infra/`、`apps/`、`config/` 包；`domain/`
  目前仅落地 `claims.py`，multipart/repetition/quota 仍在
  顶层模块；`services/` 目前仅 `delivery.py`。文档已按实际
  结构描述，不按目标布局虚构。
- D3：P7 落笔时 `companion/scheduler/__init__.py` 的
  `CareScheduler` 仍是存根（`start/stop` 抛
  `NotImplementedError`），与旧 roadmap 的 W3 表述不符，当时
  已按「未落地」如实标注。P6 接线（`9cb3a7d`）已于 P6×P7
  合并并入本 worktree，architecture/roadmap 已按「已落地」
  更新；本条保留为时点记录，不再描述现状。
- D4：P7 worktree 基线提交原为 P3+P4+P5 合并线（`b2c2bdd`）；
  本 worktree（`ai/qqbot-framework-integration2`）为 P6/P7
  合并线。生产现网运行的仍是多气泡线提交；框架线未切现网
  前，roadmap 的阶段状态与 runbook §5 的前置条件按此事实
  书写，不把框架线写成已上线。
