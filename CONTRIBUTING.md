# 贡献指南（Contributing）

欢迎提交 Issue 与 Pull Request。参与本项目即表示你同意遵守
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)；安全漏洞请按
[SECURITY.md](SECURITY.md) 私下报告，不要走公开 Issue。

## 硬规则（先看这四条）

1. **无个人标识与密钥入仓**：个人标识、密钥、邮箱地址、本地路径、真实
   webhook URL 不得进入源码、测试与文档；示例一律用占位值。
2. **测试不打真实 QQ**：用 `tests/fake_onebot.py` 写测试，测试期间不向
   真实 QQ 发送消息、也不发真实邮件。
3. **提交前跑检查**：`scripts/test.sh` 与 `ruff check .` 必须通过。
4. **危机文案只走安全配置**：危机响应文案保持固定、只通过安全配置修改，
   不经 LLM 生成或改写。

## 环境搭建

- Python **3.12 或 3.13**（`pyproject.toml` 要求 `>=3.12,<3.14`）；
- 使用 [uv](https://docs.astral.sh/uv/) 管理环境与依赖；
- 克隆后在仓库根目录同步锁定依赖：

  ```sh
  uv sync --locked
  ```

## 分支与 PR 流程

1. 从 `main` 切出主题分支，一个分支只做一件事；
2. 修改代码并补 / 改测试，保持 diff 最小、不顺手改无关模块；
3. 本地跑通 `scripts/test.sh` 与 `ruff check .`；
4. 改了配置项时，同步更新 `config.example.yaml`，并确认配置防漂移
   契约测试通过；
5. 在 `CHANGELOG.md` 的 `Unreleased` 段加一条变更说明；
6. 提交 PR，按 PR 模板逐项勾选自测清单；涉及安全链路（危机话术、
   SafetyJudge、ActionGateway 授权）的 PR 必须写明影响。

不要直接向 `main` 推送；未经维护者确认，不要把本仓库对外发布。

## 提交信息规范

格式：`type(scope): 实质描述`，参考仓库现有提交风格，例如：

```text
feat(vision): 私聊图片理解接入
fix(vision): 图片取图改 NapCat 介导主路径
docs(opensource-prep): README 中文初稿
chore(opensource-prep): stage-1 sanitization in staging tree only
```

- `type` 常用：`feat` / `fix` / `docs` / `chore` / `refactor` / `test`；
- `scope` 写受影响的模块或阶段；
- 描述写清「改了什么、为什么」，不要用「update」「fix bug」这类空话；
- 一次提交只包含一个逻辑变更。

## 测试口径

- 标准命令：`scripts/test.sh`，等价于 `uv run pytest -q --timeout=10`；
- 全部用例走 fake OneBot 夹具（`tests/fake_onebot.py`），不连接真实
  QQ、不发真实消息；
- 新增行为必须有对应测试；修 bug 时先补能复现的用例再修。

## 代码风格

- 使用 ruff：`ruff check .`，规则与行宽以 `pyproject.toml` 为准
  （`line-length = 100`，lint 规则集 `E`、`F`、`I`）；
- 格式化与导入顺序如与 ruff 检查冲突，以 `ruff check` 的结果为准。

## 许可

提交贡献即表示你同意你的贡献按本仓库的 MIT License 提供
（见 [LICENSE](LICENSE)）。
