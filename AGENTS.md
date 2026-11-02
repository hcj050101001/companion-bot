# AGENTS

本仓库是 QQ 情感陪伴机器人「阿年」（companion-bot）。

## 先读

- [CONTRIBUTING.md](CONTRIBUTING.md) — 开发约定、命令与禁止事项
- [docs/architecture.md](docs/architecture.md) — 框架化后现状架构
- [docs/roadmap.md](docs/roadmap.md) — 框架化 P0–P8 阶段状态
- [docs/napcat-ops.md](docs/napcat-ops.md) — NapCat / relay / 8090 联调
- [docs/persona-safety-v1.md](docs/persona-safety-v1.md) — 人设与危机定稿

## 硬禁止

- 密钥与真实 QQ/邮箱入库
- 绕过 safety 对外发消息；危机话术不得改由 LLM 生成
- 政治话题；冒充真人
- 未经维护者确认不对外发布（包括但不限于公开仓库、打包分发与镜像）

## 测试

`scripts/test.sh` 或 `uv run pytest -q --timeout=10`（fake OneBot，勿打真实 QQ）。
