"""Short-lived, conservative repetition state; never written to disk or logs."""

import asyncio
import re
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field

SESSION_SECONDS = 20 * 60
MAX_USERS = 1024
# Exclude requests whose interpretation or answer may depend on changing context.
_INELIGIBLE = re.compile(
    r"时间|几点|日期|今天|明天|昨天|天气|新闻|搜索|查一下|最新|"
    r"自杀|自伤|伤害|死亡|死了|痛苦|抑郁|焦虑|害怕|暴力|性侵|"
    r"不对|纠正|补充|澄清|解释一下|换个|说清楚|风格|语气|别开玩笑|"
    r"不要调侃|别调侃|停止调侃|stop teasing|time|weather|news|latest",
    re.IGNORECASE,
)
# A gentle fixed line, only used when the current tone is clearly light.
TEASE_LINE = "这个问题你问了好几遍啦，是在考我吗？😊"
_LIGHT_TONE = re.compile(
    r"哈哈|嘿嘿|嘻嘻|hhh+|笑死|好笑|好玩|逗你|开玩笑|乐死|"
    r"😄|😁|😂|🤣|😊|😉|lol|lmao|haha",
    re.IGNORECASE,
)
_STOP_TEASING = re.compile(
    r"别调侃|不要调侃|停止调侃|别开玩笑|不要开玩笑|别逗(?:我|了)?|别闹|"
    r"认真点|严肃点|停止玩笑|stop teasing|be serious",
    re.IGNORECASE,
)
# Teasing is omitted whenever distress or a sensitive topic appears, even alongside banter.
_DISTRESS = re.compile(
    r"自杀|自伤|伤害|死亡|死了|痛苦|抑郁|焦虑|害怕|难过|伤心|哭|崩溃|绝望|"
    r"暴力|性侵|创伤|生病|医院|不舒服",
    re.IGNORECASE,
)


def requests_stop_teasing(text: str) -> bool:
    """A stop request wins immediately and lasts for the ephemeral session."""
    return _STOP_TEASING.search(unicodedata.normalize("NFKC", text)) is not None


def is_light_tone(text: str, history: list[tuple[str, str]]) -> bool:
    """Require a clear light marker and no sensitive or serious context."""
    normalized = unicodedata.normalize("NFKC", text)
    recent_user_text = " ".join(
        content for role, content in history[-4:] if role == "user"
    )
    if (
        _INELIGIBLE.search(normalized)
        or _INELIGIBLE.search(recent_user_text)
        or _DISTRESS.search(normalized)
        or _DISTRESS.search(recent_user_text)
    ):
        return False
    return _LIGHT_TONE.search(normalized) is not None


def comparable_question(text: str) -> str | None:
    """Only match substantive, explicitly marked questions with identical content."""
    value = unicodedata.normalize("NFKC", text).casefold()
    value = " ".join(value.split()).strip()
    if not 4 <= len(value) <= 300 or _INELIGIBLE.search(value):
        return None
    if not (value.endswith("?") or value.endswith(("吗", "呢"))):
        return None
    # A final question mark is presentation only; preserve all interior punctuation,
    # negations, digits, names and constraints.
    return value.removesuffix("?").rstrip()


@dataclass
class _Session:
    touched: float
    turns: deque = field(default_factory=lambda: deque(maxlen=3))
    revision: int = 0
    timer: asyncio.TimerHandle | None = None


class RepetitionSessions:
    """At most three accepted comparable turns per user, with bounded users and TTL."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.sessions = OrderedDict()
        self._tease_blocked: dict[str, float] = {}

    def reset(self, user_id: str) -> None:
        session = self.sessions.pop(str(user_id), None)
        if session and session.timer:
            session.timer.cancel()

    def forget(self, user_id: str) -> None:
        """Forget commands erase both repeat state and any stop-teasing block."""
        self.reset(user_id)
        self._tease_blocked.pop(str(user_id), None)

    def close(self) -> None:
        for user_id in list(self.sessions):
            self.reset(user_id)
        self._tease_blocked.clear()

    def block_teasing(self, user_id: str) -> None:
        now, user_id = self.clock(), str(user_id)
        for key, expiry in list(self._tease_blocked.items()):
            if expiry <= now:
                self._tease_blocked.pop(key, None)
        if len(self._tease_blocked) >= MAX_USERS and user_id not in self._tease_blocked:
            self._tease_blocked.pop(next(iter(self._tease_blocked)))
        self._tease_blocked[user_id] = now + SESSION_SECONDS

    def allow_teasing(self, user_id: str) -> bool:
        now, user_id = self.clock(), str(user_id)
        expiry = self._tease_blocked.get(user_id)
        if expiry is None:
            return True
        if now < expiry:
            return False
        self._tease_blocked.pop(user_id, None)
        return True

    def begin(self, user_id: str, text: str) -> tuple[int, tuple | None]:
        now, user_id = self.clock(), str(user_id)
        for key, session in list(self.sessions.items()):
            if now - session.touched >= SESSION_SECONDS:
                self.reset(key)
        question = comparable_question(text)
        if question is None:
            self.reset(user_id)
            return 0, None
        session = self.sessions.get(user_id)
        if session is None:
            if len(self.sessions) >= MAX_USERS:
                self.reset(next(iter(self.sessions)))
            session = self.sessions[user_id] = _Session(now)
        session.touched = now
        self._schedule_expiry(user_id, session)
        self.sessions.move_to_end(user_id)
        session.revision += 1
        previous = list(session.turns)
        level = min(len(previous), 2) if previous and previous[-1] == question else 0
        if level == 0:
            session.turns.clear()
        return level, (session, session.revision, question)

    def _schedule_expiry(self, user_id: str, session: _Session) -> None:
        if session.timer:
            session.timer.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            session.timer = None
            return

        def expire():
            if self.sessions.get(user_id) is session:
                self.reset(user_id)

        session.timer = loop.call_later(SESSION_SECONDS, expire)

    def finish(self, user_id: str, token: tuple | None, accepted: bool) -> None:
        if token is None:
            return
        session, revision, question = token
        if self.sessions.get(str(user_id)) is not session or session.revision != revision:
            return
        if not accepted:
            self.reset(user_id)
            return
        session.turns.append(question)
        session.touched = self.clock()
        self._schedule_expiry(str(user_id), session)
