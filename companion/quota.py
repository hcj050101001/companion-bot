"""Shared outbound send accounting used by the reply path and the OneBot gateway.

One per-user normal bucket (18 attempts / rolling 60s) is shared by ordinary
replies, onboarding, and care whenever they use the normal gateway. One account
bucket holds 60 normal attempts plus 10 separately reserved critical attempts per
rolling 60s. At most two outbound requests may be in flight, at most one of them
normal; the second slot is reserved for critical traffic.

Two-level model (plan C8, N2): the account buckets are the unbreakable top
level — the 60 normal attempts are a single shared pool and the 10 critical
attempts are a reserved pool that normal traffic can never touch. Group sends
form a second-level sub-bucket (per-group 10 plus an account-wide group
sub-budget of 20 per rolling 60s) that *also* draws from the shared account
normal pool, so private and group traffic combined can never exceed the
account cap, and saturating either side starves the other rather than
exceeding the total.
"""

import asyncio
import threading
import time
from collections import defaultdict, deque

NORMAL_PER_USER_LIMIT = 18
NORMAL_PER_USER_WINDOW_SECONDS = 60
ACCOUNT_NORMAL_LIMIT = 60
ACCOUNT_CRITICAL_LIMIT = 10
GROUP_PER_GROUP_LIMIT = 10
ACCOUNT_GROUP_LIMIT = 20
ACCOUNT_WINDOW_SECONDS = 60
MAX_CONCURRENT_OUTBOUND = 2
MAX_CONCURRENT_NORMAL = 1
SLOT_WAIT_SECONDS = 2.0


class QuotaUnavailable(Exception):
    """Shared accounting cannot make a trustworthy decision; callers must fail closed."""


class QuotaToken:
    """A single pre-reserved attempt handed to the gateway to avoid a double charge."""

    __slots__ = ("quota", "user_id", "critical", "spent", "accepted", "scope")

    def __init__(
        self, quota: "SendQuota", user_id: str, critical: bool, scope: str = "user"
    ):
        self.quota, self.user_id, self.critical = quota, user_id, critical
        self.scope = scope
        self.spent = True
        self.accepted = False


class QuotaReservation:
    """Slots reserved atomically for one validated multipart answer."""

    __slots__ = ("quota", "user_id", "remaining", "critical", "scope", "_lock")

    def __init__(
        self, quota: "SendQuota", user_id: str, count: int, critical: bool,
        scope: str = "user",
    ):
        self.quota, self.user_id, self.remaining, self.critical = (
            quota, user_id, count, critical
        )
        self.scope = scope
        self._lock = threading.Lock()

    def consume(self) -> QuotaToken | None:
        with self._lock:
            if self.remaining <= 0:
                return None
            self.remaining -= 1
            return QuotaToken(self.quota, self.user_id, self.critical, self.scope)


class SendQuota:
    """Process-local rolling-window attempts and bounded outbound concurrency."""

    def __init__(
        self,
        *,
        clock=time.monotonic,
        normal_per_user: int = NORMAL_PER_USER_LIMIT,
        account_normal: int = ACCOUNT_NORMAL_LIMIT,
        account_critical: int = ACCOUNT_CRITICAL_LIMIT,
        group_per_group: int = GROUP_PER_GROUP_LIMIT,
        account_group: int = ACCOUNT_GROUP_LIMIT,
        window_seconds: int = ACCOUNT_WINDOW_SECONDS,
        slot_wait_seconds: float = SLOT_WAIT_SECONDS,
    ):
        self.clock = clock
        self.normal_per_user = normal_per_user
        self.account_normal = account_normal
        self.account_critical = account_critical
        self.group_per_group = group_per_group
        self.account_group = account_group
        self.window_seconds = window_seconds
        self.slot_wait_seconds = slot_wait_seconds
        self._lock = threading.Lock()
        self._user_normal: dict[str, deque[float]] = defaultdict(deque)
        self._group_normal: dict[str, deque[float]] = defaultdict(deque)
        self._account_group: deque[float] = deque()
        self._account_normal: deque[float] = deque()
        self._account_critical: deque[float] = deque()
        self._normal_sem = asyncio.Semaphore(MAX_CONCURRENT_NORMAL)
        self._total_sem = asyncio.Semaphore(MAX_CONCURRENT_OUTBOUND)

    @staticmethod
    def _prune(bucket: deque[float], now: float, window: float) -> None:
        while bucket and bucket[0] <= now - window:
            bucket.popleft()

    def snapshot(self) -> dict:
        """只读余量快照：加锁 prune 两个账户桶后返回剩余量（§5.10）。

        不预留、不消费任何额度，既有记账语义不变。只 prune 账户级
        桶：per-user 桶由 reserve 路径惰性 prune，snapshot 不遍历
        全部用户桶，避免抓取时持锁 O(用户数) 阻塞发送预留
        （Review P4-4）。
        """
        with self._lock:
            now = self.clock()
            self._prune(self._account_normal, now, self.window_seconds)
            self._prune(self._account_critical, now, self.window_seconds)
            return {
                "normal_remaining": max(
                    0, self.account_normal - len(self._account_normal)
                ),
                "critical_remaining": max(
                    0, self.account_critical - len(self._account_critical)
                ),
                "per_user_normal_limit": max(0, self.normal_per_user),
            }

    def group_snapshot(self) -> dict:
        """只读群子桶余量快照（C8 两级模型的第二级），口径同 snapshot：
        只 prune 账户级桶，不遍历全部群桶、不预留不消费。"""
        with self._lock:
            now = self.clock()
            self._prune(self._account_group, now, self.window_seconds)
            return {
                "group_remaining": max(
                    0, self.account_group - len(self._account_group)
                ),
                "per_group_limit": max(0, self.group_per_group),
                "account_group_limit": max(0, self.account_group),
            }

    def reserve(
        self,
        user_id: str,
        slots: int = 1,
        *,
        critical: bool = False,
        allow_partial_single: bool = False,
        scope: str = "user",
    ) -> QuotaReservation | None:
        """Atomically reserve all requested attempts, or one when partial is allowed.

        scope="user"（默认）：per-user 子桶 + 账号普通总池。
        scope="group"（C8）：per-group 子桶 + 账号群发子预算 + 同一个
        账号普通总池——群发与私聊共享总池，两边合计永不超过账号上限；
        群发永不触碰 critical 预留池，critical 预留也只走 critical 路径。
        """
        if not isinstance(slots, int) or slots < 1:
            raise ValueError("slots must be a positive integer")
        if scope not in {"user", "group"}:
            raise ValueError("unknown quota scope")
        if critical and scope == "group":
            # critical 是私聊告警专属（C8：不可自声明提级）：记账层
            # 直接拒群域 critical，不让下层发送面拿到这种 token
            # （Codex Review N2 #4）。
            return None
        try:
            now = self.clock()
            user_id = str(user_id)
            with self._lock:
                self._prune(self._account_normal, now, self.window_seconds)
                self._prune(self._account_critical, now, self.window_seconds)
                if critical:
                    available = self.account_critical - len(self._account_critical)
                    if available < slots and not (allow_partial_single and available >= 1):
                        return None
                    granted = slots if available >= slots else 1
                    self._account_critical.extend([now] * granted)
                    return QuotaReservation(self, user_id, granted, True)
                if scope == "group":
                    self._prune(self._account_group, now, self.window_seconds)
                    group_bucket = self._group_normal[user_id]
                    self._prune(group_bucket, now, self.window_seconds)
                    available = min(
                        self.group_per_group - len(group_bucket),
                        self.account_group - len(self._account_group),
                        self.account_normal - len(self._account_normal),
                    )
                    if available < slots and not (
                        allow_partial_single and available >= 1
                    ):
                        return None
                    granted = slots if available >= slots else 1
                    group_bucket.extend([now] * granted)
                    self._account_group.extend([now] * granted)
                    self._account_normal.extend([now] * granted)
                    return QuotaReservation(self, user_id, granted, False, "group")
                user_bucket = self._user_normal[user_id]
                self._prune(user_bucket, now, self.window_seconds)
                available = min(
                    self.normal_per_user - len(user_bucket),
                    self.account_normal - len(self._account_normal),
                )
                if available < slots and not (allow_partial_single and available >= 1):
                    return None
                granted = slots if available >= slots else 1
                user_bucket.extend([now] * granted)
                self._account_normal.extend([now] * granted)
                return QuotaReservation(self, user_id, granted, False)
        except Exception as exc:  # noqa: BLE001 — accounting must fail closed
            raise QuotaUnavailable("shared send accounting unavailable") from exc

    def accept_token(
        self,
        token: QuotaToken | None,
        *,
        critical: bool,
        identity: str | None = None,
        scope: str | None = None,
    ) -> bool:
        """Validate and consume a pre-reserved attempt exactly once.

        scope（N2，Codex Review #6）：token 记录预留时的计费域；
        调用方给出期望域时必须一致——user 域预留不得冒充群发
        （否则群子桶被整体绕过）。
        """
        with self._lock:
            if (
                not isinstance(token, QuotaToken)
                or token.quota is not self
                or not token.spent
                or token.critical is not critical
                or token.accepted
                or (identity is not None and token.user_id != str(identity))
                or (scope is not None and token.scope != scope)
            ):
                return False
            token.accepted = True
            return True

    async def acquire_slot(self, critical: bool) -> bool:
        """Acquire one outbound slot with a bounded wait; critical never waits for normal."""
        timeout = self.slot_wait_seconds
        if critical:
            try:
                await asyncio.wait_for(self._total_sem.acquire(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        acquired_normal = False
        try:
            await asyncio.wait_for(self._normal_sem.acquire(), timeout=timeout)
            acquired_normal = True
            await asyncio.wait_for(self._total_sem.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            if acquired_normal:
                self._normal_sem.release()
            return False

    def release_slot(self, critical: bool) -> None:
        if critical:
            self._total_sem.release()
        else:
            self._normal_sem.release()
            self._total_sem.release()
