# E2b 验收说明（`63cefc0`）

## 目标
危机模型 judge **超时 / 失败 / 不可解析**时：至少重试 1 次；仍失败按 **「疑似危机」** 走固定危机话术 + 本地告警，**不得**静默只靠关键词。

## 实现位置
| 项 | 路径 |
|----|------|
| `JudgeUnavailable` + 重试 | `companion/safety/__init__.py` → `ModelLLMJudge.judge`（`range(2)`） |
| 降级为疑似危机 | `SafetyGateway.detect` 捕获 `JudgeUnavailable` → `(True, "疑似危机")` |
| 边界说明 | `docs/persona-safety-v1.md` §E2b |
| 单测 | `tests/test_app.py`（E2b 相关用例）；全量 `scripts/test.sh` |

## 验收路径（给测试）
1. **单测门禁**：`scripts/test.sh` 全绿（`63cefc0` 基线 79 passed）。
2. **失败重试**：注入 judge 第一次抛错/超时、第二次成功 → 不触发危机（或按第二次 YES/NO）。
3. **仍失败降级**：两次均失败 / 均返回不可解析 / 均等于 `FALLBACK_REPLY` → 用户收到方案 2 固定危机话术；`logs/alerts.jsonl` 有记录；原因类别为「疑似危机」。
4. **关键词仍优先**：显式危机关键词不依赖 judge，直接危机通道。
5. **对照**：`UnknownLLMJudge` 路径仍仅关键词兜底（测试/旧行为），与生产 `ModelLLMJudge` 区分。
6. **隐晦召回（产品口径）**：judge **可用**时，隐晦样例召回 ≥90%（样例集见 `tests/fixtures/crisis_samples_avoidant.json`；原内部召回记录 `tests/W1-avoidant-recall.md` 已在开源清洗中移除）；E2b 本身不替代该召回指标。

## 手工冒烟（可选，白名单账号）
- 普通寒暄：正常陪伴回复（judge 返回 NO）。
- 断网或临时弄坏 `DEEPSEEK_API_KEY`：非关键词消息也应收到危机固定话术（疑似危机），而非普通闲聊或静默。
- 恢复 key 后热加载/重启，寒暄恢复正常。

## 非目标
- 不改变方案 2 话术文案；不因 E2b 承诺通知真人。
- 不把真实 `ONEBOT_SECRET` / API key 写入仓库或报告明文。

## 正式验收报告
测试完整报告（召回/重试/告警）为内部验收材料，已在开源清洗中移除（原路径 `tests/reports/retest-63cefc0-E2b.md`，含 stub `e2b-63cefc0.md`）；公开口径以本文的验收路径与结论为准。

