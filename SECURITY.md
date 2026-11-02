# 安全政策（Security Policy）

本项目是一个会对外发送消息、调用工具并处理用户对话内容的机器人框架，
安全问题的影响面比普通库更大，请按本文件的方式负责任地披露。

## 支持的版本

本项目在首个公开版本发布之前，只对最新状态提供安全修复：

| 版本 | 是否支持 |
| --- | --- |
| 最新提交（`main` 分支 HEAD） | ✅ 支持 |
| 更早的提交 / 未打 tag 的历史快照 | ❌ 不支持，请先在最新提交上复现 |

首个公开版本发布后，本表将改为按已发布版本号列出支持范围。

## 如何报告漏洞

请**不要**用公开 Issue 报告安全漏洞，也不要在 Issue / PR / 讨论区里粘贴
利用细节、真实 QQ 号、密钥或生产日志原文。

报告渠道：**GitHub 私有安全公告（GitHub Security Advisories）**——在仓库的
Security 页选择 “Report a vulnerability” 私下提交。

>
> 上述渠道在公开仓库建立后才正式生效；在此之前本文件为演练稿，
> 不构成对外承诺的联系方式。本文件不提供任何邮箱地址。

报告时请尽量包含：受影响的版本 / 提交、复现步骤、影响范围（能读到什么、
能发什么、能绕过哪一层校验），以及你已验证过的修复思路（如有）。

## 最高优先级：危机与安全链路

以下类别的问题按**最高优先级**处理，请在报告标题中注明类别：

- **危机话术与危机响应链路**：危机文案被改写、绕过、延迟或错误触发，
以及危机响应被普通对话 / LLM 输出覆盖的问题；
- **SafetyJudge（安全判定）**：安全判定被绕过、降级或在无结论时未按
保守口径处理的问题；
- **ActionGateway 授权绕过类**：未经授权的主体发出消息或执行动作、
分级 / 授权 / 一次性确认令牌被绕过、审计账本缺失或可被篡改的问题。

这类问题一旦确认，会优先于普通功能缺陷修复。

## 响应时限（非承诺）

我们会**尽力在 7 天内确认收到报告**，并在确认后尽力及时给出初步评估与
修复计划。维护者人数有限，时限为目标而非承诺；若长时间未收到回复，
可通过同一渠道再次留言提醒。在修复发布前，请为维护者保留合理的
披露窗口，不要提前公开细节。

## English Summary

**Security Policy.** Before the first public release, only the latest commit
on the `main` branch receives security fixes; please reproduce issues there
first. Report vulnerabilities privately via **GitHub Security Advisories**
(“Report a vulnerability” on the repository’s Security tab) — never through
public issues, and never paste real QQ IDs, secrets, or raw production logs.
*[Placeholder: this channel takes effect once the public repository exists;
the reporting channel is pending the maintainer’s decision, and no email
address is provided.]* Issues affecting the crisis-response path (fixed
crisis copy and the SafetyJudge) or authorization bypasses in the
ActionGateway are treated as highest priority. We will try to acknowledge
reports within 7 days; this is a best-effort target, not a guarantee.
Please allow a reasonable disclosure window before publishing details.
