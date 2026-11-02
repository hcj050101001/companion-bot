"""危机识别、白名单和告警接口。"""

import asyncio
import json
import logging
import os
import re
import smtplib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from companion.config import EnvironmentSecretProvider


class JudgeUnavailable(Exception):
    """模型危机判断在重试后仍不可用；网关应按疑似危机处理（E2b）。"""


class LLMJudge(ABC):

    """可插拔模型危机判断器；返回 True/False/None（未知）。"""

    @abstractmethod
    async def judge(self, text: str) -> bool | None:
        """判断文本是否包含危机信号。"""


class UnknownLLMJudge(LLMJudge):
    """默认判断器，不做模型推断。"""

    async def judge(self, text: str) -> bool | None:
        return None


class Alerter(ABC):
    """危机告警接口。"""

    @abstractmethod
    async def alert(self, user_id: str, reason: str) -> None:
        """发送告警；实现不得写入用户原文。"""


class JsonlAlerter(Alerter):
    """将不含原文的危机告警追加到 JSONL。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def alert(self, user_id: str, reason: str, summary: str = "安全风险已触发") -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "category": reason,
            "user_id": str(user_id),
            "summary": summary,
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logging.getLogger("companion.alerts").error("本地告警写入失败: %s", type(exc).__name__)
            raise


class EmailAlerter(Alerter):
    """先写本地脱敏记录，再按可选配置发送 Gmail SMTP。"""

    def __init__(self, fallback: JsonlAlerter, secret_provider=None):
        self.fallback = fallback
        self.secret_provider = (
            secret_provider if secret_provider is not None else EnvironmentSecretProvider()
        )

    async def alert(self, user_id: str, reason: str, summary: str = "安全风险已触发") -> None:
        await self.fallback.alert(user_id, reason, summary)
        # ALERT_EMAIL_TO 是收件地址、不是密钥，不在 provider 名册内，保持从进程环境读取。
        password = self.secret_provider.get("GMAIL_APP_PASSWORD")
        recipient = os.getenv("ALERT_EMAIL_TO")
        if not password or not recipient:
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        message = EmailMessage()
        message["Subject"] = "陪伴机器人安全告警"
        message["From"], message["To"] = recipient, recipient
        message.set_content(
            f"time: {timestamp}\nuser_id: {user_id}\ntrigger_type: {reason}\n{summary}\n"
        )

        def send_email():
            with smtplib.SMTP_SSL("smtp." + "gmail" + ".com", 465, timeout=10) as smtp:
                smtp.login(recipient, password)
                smtp.send_message(message)

        try:
            await asyncio.to_thread(send_email)
        except Exception as exc:
            logging.getLogger("companion.alerts").error("邮件告警失败: %s", type(exc).__name__)


class ModelLLMJudge(LLMJudge):
    """复用回复 provider 做危机分类，不暴露原文到告警。"""

    def __init__(self, client):
        self.client = client

    async def judge(self, text: str) -> bool | None:
        """至少尝试 2 次；全部失败则抛 JudgeUnavailable（E2b 保守降级）。"""
        from companion.brain import FALLBACK_REPLY

        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    self.client.generate(
                        "判断以下用户消息是否有自伤、自杀或即时人身安全风险。仅回答 YES 或 NO。\n"
                        + text
                    ),
                    timeout=20,
                )
                if result == FALLBACK_REPLY:
                    last_error = RuntimeError("fallback_reply")
                    continue
                answer = result.strip().upper()
                if answer.startswith("YES"):
                    return True
                if answer.startswith("NO"):
                    return False
                last_error = RuntimeError("unparsed_judge_reply")
            except Exception as exc:  # noqa: BLE001 — 需统一重试后降级
                last_error = exc
                continue
        logging.getLogger("companion.safety").warning(
            "危机 judge 重试后仍失败: %s", type(last_error).__name__ if last_error else "unknown"
        )
        raise JudgeUnavailable("crisis judge unavailable after retry")


class SafetyGateway:
    """规则兜底加模型判断；任一命中即触发。"""

    def __init__(
        self,
        keywords: list[str],
        judge: LLMJudge | None = None,
        categories: list[dict] | None = None,
    ):
        self.keywords = [str(k).casefold() for k in keywords if str(k) != "准备好了"]
        self.judge = judge or UnknownLLMJudge()
        self.categories = categories or []

    def keyword_match(self, text: str) -> str | None:
        """关键词危机预筛：命中返回分类名（无分类时 "危机"），未命中返回 None。

        含无害短语掩码与分类目判定，与 detect 的关键词路径逐项等价；供接入层
        在调用模型 judge 前预筛分道（critical 道）使用。
        """
        folded = text.casefold()
        # Mask known harmless phrases in place, preserving other signals in the same message.
        for harmless in (
            "笑死我了",
            "笑死了",
            "笑死",
            "这游戏想死",
            "游戏里想死",
            "游戏里角色想自杀剧情",
            "想死了这道题",
            "活着没意思？开个玩笑",
            "伤害自己的习惯是咬指甲",
            "准备好了明天出门",
            "不想死",
        ):
            folded = folded.replace(harmless, " " * len(harmless))

        def matches(phrase: str) -> bool:
            # Chinese does not use spaces between words: recognize explicit sentence
            # boundaries and a small set of common subjects/modifiers on the left.
            # Sentence particles must themselves end at a boundary.
            pattern = (
                r"(?:^|[^\w]|我要|我想|我|你|他|她|自己|真的|已经|现在|总是|觉得|好像|非常|就是)"
                + re.escape(phrase.casefold())
                + r"(?=$|[^\w]|(?:了|啊|呀|呢|吧|嘛|吗|怎么办)(?=$|[^\w]))"
            )
            return re.search(pattern, folded) is not None

        match = next((kw for kw in self.keywords if matches(kw)), None)
        if not match:
            return None
        category = next(
            (
                item
                for item in self.categories
                if any(k != "准备好了" and matches(k) for k in item.get("keywords", []))
            ),
            None,
        )
        return str(category.get("name", "keyword")) if category else "危机"

    async def detect(self, text: str) -> tuple[bool, str]:
        category = self.keyword_match(text)
        if category is not None:
            return True, category
        try:
            result = await self.judge.judge(text)
        except Exception:
            # D1 保守降级：judge 抛任何异常（JudgeUnavailable 或普通异常）
            # 一律按疑似危机处理，不得静默放行。
            return True, "疑似危机"
        return (True, "model") if result is True else (False, "")


def crisis_reply(
    helplines: list[str], notify_enabled: bool = False, safety: dict | None = None
) -> str:
    """生成固定且不依赖模型的危机求助话术。"""
    numbers = "、".join(helplines)
    copy = (safety or {}).get("crisis_copy", {})
    variant = "notify_enabled" if notify_enabled else "notify_disabled"
    template = copy.get(variant, "请拨打热线：{helplines}。")
    return template.format(helplines=numbers)
