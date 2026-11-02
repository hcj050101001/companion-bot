## 变更说明 / Summary

<!-- 这个 PR 做了什么、为什么？关联 issue 时写明（如 Closes #123）。
     What does this PR change, and why? Link issues where relevant. -->

## 影响范围 / Impact

<!-- 涉及哪些模块 / 配置 / 行为变化；没有影响的面也请写明「无」。
     Affected modules, config keys, and behavior changes. -->

## 自测清单 / Checklist

- [ ] 已运行 `scripts/test.sh` 且全部通过（即 `pytest -q --timeout=10`）
- [ ] 已运行 `ruff check .`
- [ ] 无密钥、真实 QQ 号 / 邮箱、真实 webhook URL、本地路径入仓
      （源码、测试、文档、fixture 均已检查）
- [ ] 涉及配置变更时：已同步 `config.example.yaml`，且配置防漂移契约测试通过
- [ ] 已在 `CHANGELOG.md` 的 `Unreleased` 段添加条目
- [ ] 新增 / 变更行为有对应测试，且测试使用 `tests/fake_onebot.py`，
      不向真实 QQ 发送消息

## 安全链路 / Safety impact

- [ ] 本 PR 不涉及安全链路（危机话术、SafetyJudge、ActionGateway 授权）
- [ ] 本 PR 涉及安全链路，影响说明如下：

<!-- 涉及危机文案时：确认文案仍只走安全配置、不经 LLM 生成。
     涉及授权时：说明主体校验、分级与审计是否保持完整。 -->
