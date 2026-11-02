"""断线重连补回编排：触发观察者 ``RecoveryWatcher`` + 扫描服务 ``CatchupService``。

设计见实例设计稿 §4/§5（qqbot-reconnect-catchup）：连接健康由坏转好
且稳定 ≥120s 时触发一次补回扫描，从 NTQQ 本地历史拉取断线窗口内
未处理的消息，按会话合成一个补处理回合走既有管线回复。

纪律（逐条对应设计钉死口径）：

- 本模块只读健康模块的公开状态（snapshot / stable_since / clock），
  不改健康模块、不重启任何进程、不碰登录态（§4.4）；
- 历史原始返回的解读全部在单一适配层 ``companion.adapters.history``
  （D1 隔离点）——本模块**不出现**三个历史动作的名字符串、**不访问**
  ActionResult 原始 ``data``，只消费适配层的归一化类型与
  ``HistorySource`` 协议（AC27 编排侧红线）；
- 私聊回合只经 ``processor.dispatch_parsed`` 进入既有 Stage 链，
  群回合只经 ``companion.ingress.events.handle_group_message`` 进入
  既有群子链，不新开第二条回复链（AC12 / 解释 E1）；
- 本模块不写任何危机分支：私聊危机由既有 Stage 链处置，群危机沿
  群链观察口径（解释 E2）；限流/额度不开豁免，回合一律经既有入口；
- 落盘审计只用 §5.8 白名单键，会话标识只落 ``session_hash``，
  绝不落消息正文与原始会话标识（AC25 / 解释 E4）；
- ``processor`` 按不透明句柄使用（依赖注入纪律，§5.1），不 import
  ``companion.ingress`` 包 ``__init__`` 的实现细节。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from companion.adapters.base import TextSegment
from companion.domain.claims import (
    LANE_GROUP,
    LANE_ORDINARY,
    ClaimOutcome,
    classify_lane,
)
from companion.ops.connection_health import STABLE_SECONDS
from companion.perception import UserMessage

from .events import (
    group_reply_subject_admitted,
    group_subject,
    handle_group_message,
    is_group_trigger,
)

if TYPE_CHECKING:  # 归一化类型只作标注；运行时按协议鸭子类型消费
    from companion.adapters.history import HistorySource

logger = logging.getLogger("companion.catchup")

# ---------------------------------------------------------------------------
# 常量表（设计 §5.9 全部钉死，不许自创数值）
# ---------------------------------------------------------------------------

MAX_WINDOW_SECONDS = 86400.0  # 24h 窗口硬上限（D4 / AC4）
PER_CONVERSATION_LIMIT = 10  # 每会话合成回合取最新条数（D4 / AC14）
MAX_CONVERSATIONS_PER_SCAN = 20  # 单轮扫描会话上限（D4 / AC24）
FALLBACK_MAX_CONVERSATIONS = 20  # 降级发现试探上限（OD7 钉死，与单轮上限同额）
SCAN_TOTAL_TIMEOUT_SECONDS = 120.0  # 整轮扫描总时限（OD3 钉死）
HISTORY_CALL_TIMEOUT_SECONDS = 15.0  # 单次历史拉取上限，受扫描剩余时限钳制
POLL_INTERVAL_SECONDS = 5.0  # Watcher 轮询间隔（§4.1）
MIN_WINDOW_SECONDS = 1.0  # 窗口宽度下限：小于此视为无事不触发（§4.3）
# 会话审计 extra 白名单（§5.8 / E4）：只许计数类元数据，原始消息/会话
# 标识一律不得经 extra 落盘（复审轮 2 P4 修复，audit() 处强制过滤）。
_AUDIT_EXTRA_KEYS = frozenset({"selected_count"})
PLATFORM = "onebot"  # claim 键平台分量，与 normalize 产出一致


@dataclass(frozen=True)
class CatchupSettings:
    """补回设置快照：由组合根从配置段 ``reconnect_catchup`` 投影（§7.2）。

    普通 dataclass（非 Pydantic 对象直传）；默认值与
    ``ReconnectCatchupConfig`` 逐字一致，开关默认关。
    """

    enabled: bool = False
    prefix_enabled: bool = True
    prefix_text: str = "（刚信号不好，才看到你之前的消息）"


class _ScanDeadlineExceeded(Exception):
    """派发等待耗尽整轮扫描剩余时限（内部信号）。

    由逐会话处理在派发 await 处抛出，``run_scan`` 据此把整轮 outcome
    置 timeout 后收尾——否则单个挂死回合会让 run_scan 无限期不返回、
    本窗后续会话静默丢失（审查 P3 修复）。
    """


@dataclass(frozen=True)
class ScanReport:
    """一轮扫描的进程内报告（不落盘；含原始会话标识，见 E4）。"""

    window_start: float
    window_end: float
    outcome: str  # completed | timeout | store_blocked | disabled
    conversations_seen: int = 0
    conversations_processed: int = 0  # 实际发起了回合的会话数
    conversations_truncated: int = 0  # 因 20 上限被截断的会话数
    turns_dispatched: int = 0
    entries_selected: int = 0
    entries_dropped: dict[str, int] = field(default_factory=dict)  # 原因码→计数
    sessions: tuple[str, ...] = ()  # 实际处理的会话标识（仅进程内，E4）


def _session_hash(session_id: str) -> str:
    """落盘审计用会话哈希：sha256(session_id) 前 16 位十六进制（E4）。"""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _healthy_now(snapshot) -> bool:
    """健康谓词：与健康模块内部口径逐字对齐（§4.1）。"""
    return (
        snapshot is not None
        and snapshot.ingress == "ready"
        and snapshot.qq == "reported_online"
        and snapshot.companion_process == "active"
        and snapshot.napcat_process == "active"
    )


def _wrap_group_responder(responder, prefix):
    """群 responder 前缀包装（§5.7 第 7 点，ADR-10）：不改 handle_group_message。

    prefix 为 None 或 responder 为 None 时原样返回；否则在原产出前
    拼前缀——str 直接拼接，单个段包装为二元组，可迭代产出在首位插入
    纯文本前缀段，None/空产出原样返回（无回复即无前缀）。危机关键词
    命中的群回合在 observe 段即被跳过、不到 responder，前缀无从附着。
    """
    if prefix is None or responder is None:
        return responder

    async def _wrapped(ctx):
        produced = responder(ctx)
        if inspect.isawaitable(produced):
            produced = await produced
        if produced is None:
            return produced
        if isinstance(produced, str):
            return prefix + produced if produced else produced
        if isinstance(produced, (TextSegment,)) or not _is_iterable(produced):
            return (TextSegment(prefix), produced)
        items = tuple(produced)
        if not items:
            return produced
        return (TextSegment(prefix), *items)

    return _wrapped


def _is_iterable(value) -> bool:
    try:
        iter(value)
    except TypeError:
        return False
    return True


# ---------------------------------------------------------------------------
# RecoveryWatcher（设计 §4.1 状态机）
# ---------------------------------------------------------------------------


class RecoveryWatcher:
    """轮询健康状态、自维护「由坏转好且稳定 ≥120s」转移状态机。

    时钟分工：稳定性用健康对象的 ``clock()``（单调钟，与 stable_since
    同源）；窗口时刻用注入的 ``wall_clock``（epoch 秒，与历史条目
    发送时刻同域）。``service`` 只要求有 ``run_scan`` 协程方法与
    ``scan_store`` 属性（窗口钳制要读 ``latest_window_end``）。
    """

    def __init__(self, health_provider, service, *, wall_clock=time.time, sleep=asyncio.sleep):
        self._health_provider = health_provider
        self._service = service
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._down_since_wall: float | None = None
        self._startup_pending = True  # 构造时 True（启动恢复口径 §4.3）
        self._scan_task: asyncio.Task | None = None
        self._pending_window: tuple[float, float] | None = None

    # -- 内部 ---------------------------------------------------------------

    def _scan_in_flight(self) -> bool:
        if self._scan_task is not None and self._scan_task.done():
            self._scan_task = None
        return self._scan_task is not None

    def _latest_window_end(self) -> float:
        store = getattr(self._service, "scan_store", None)
        if store is None:
            return 0.0
        try:
            return float(store.latest_window_end())
        except Exception as exc:  # noqa: BLE001 — 读标记失败按无记录处理，不阻断触发判定
            logger.warning("catchup latest_window_end failed: %s", type(exc).__name__)
            return 0.0

    def _compute_window(self, candidate: float, recovered_at: float) -> tuple[float, float]:
        """窗口唯一口径（§4.2）：下限三者取 max，窗口不重叠、崩溃不重扫。"""
        window_start = max(
            candidate,
            self._latest_window_end(),
            recovered_at - MAX_WINDOW_SECONDS,
        )
        return window_start, recovered_at

    def _launch(self, window: tuple[float, float]) -> None:
        self._scan_task = asyncio.create_task(self._service.run_scan(*window))

    def _launch_pending(self) -> bool:
        """发出在途期间保留的待触发窗口（下限再与最新已扫终点合并，§4.1 规则 4）。"""
        if self._pending_window is None or self._scan_in_flight():
            return False
        pending_start, pending_end = self._pending_window
        self._pending_window = None
        window_start = max(pending_start, self._latest_window_end())
        if pending_end - window_start < MIN_WINDOW_SECONDS:
            return False
        self._launch((window_start, pending_end))
        return True

    # -- 公开 ---------------------------------------------------------------

    async def poll_once(self) -> bool:
        """采样一次健康状态并按 §4.1 转移规则推进；返回是否受理/发起了扫描。"""
        if self._launch_pending():
            return True
        health = self._health_provider()
        if health is None:  # 健康开关关：Watcher 惰性（§8.3 接线期已告警）
            return False
        snapshot = getattr(health, "snapshot", None)
        if snapshot is None:  # 尚未首测
            return False
        if not _healthy_now(snapshot):
            if self._down_since_wall is None:
                self._down_since_wall = self._wall_clock()
            # 启动恢复让位于真实观测到的断线（§4.1 规则 2）
            self._startup_pending = False
            return False
        stable_since = getattr(health, "stable_since", None)
        if stable_since is None or health.clock() - stable_since < STABLE_SECONDS:
            return False  # 未稳定：不触发、不清 _down_since_wall（AC1 前半）
        # 已稳定：本转移只在此处被消费一次（AC1 后半 / AC2 前半）
        if self._down_since_wall is not None:
            candidate = self._down_since_wall
            self._down_since_wall = None
        elif self._startup_pending:
            # 启动恢复口径（§4.3）：候选下限 = recovered_at − 24h，再被
            # §4.2 的 latest_window_end 钳制；钳制后窗口过小则不触发。
            candidate = self._wall_clock() - MAX_WINDOW_SECONDS
            self._startup_pending = False
        else:
            return False
        recovered_at = self._wall_clock()
        window = self._compute_window(candidate, recovered_at)
        if window[1] - window[0] < MIN_WINDOW_SECONDS:
            return False  # 视为无事（转移已消费，不反复评估同一转移）
        if self._scan_in_flight():
            # 在途扫描时只保留最新一个待触发窗口（§4.1 规则 4）
            self._pending_window = window
            return True
        self._launch(window)
        return True

    async def run(self) -> None:
        """5s 轮询循环（经 InProcessScheduler 托管，§8.3）。"""
        while True:
            await self.poll_once()
            await self._sleep(POLL_INTERVAL_SECONDS)

    async def wait_idle(self) -> None:
        """等待在途与 pending 扫描全部落定（供测试与关停）。"""
        while True:
            if self._scan_task is not None:
                task, self._scan_task = self._scan_task, None
                try:
                    await task
                except Exception as exc:  # noqa: BLE001 — 等待方不因扫描失败而失败
                    logger.warning("catchup scan task failed: %s", type(exc).__name__)
                continue
            if self._launch_pending():
                continue
            return


# ---------------------------------------------------------------------------
# CatchupService（设计 §5 扫描编排）
# ---------------------------------------------------------------------------


class CatchupService:
    """一轮补回扫描的编排：发现 → 过滤 → 已处理核查 → 合成回合 → 既有入口派发。"""

    def __init__(
        self,
        processor,
        *,
        history: HistorySource,
        claim_store,
        scan_store,
        ledger,
        config: CatchupSettings,
        wall_clock=time.time,
        monotonic=time.monotonic,
        sleep=asyncio.sleep,
    ):
        self._processor = processor
        self._history = history
        self._claim_store = claim_store
        # 公开属性：RecoveryWatcher 经此读 latest_window_end 做窗口钳制（§4.2）
        self.scan_store = scan_store
        self._ledger = ledger
        self._config = config
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._sleep = sleep

    # -- 主流程（§5.2） ------------------------------------------------------

    async def run_scan(self, window_start: float, window_end: float) -> ScanReport:
        started = self._monotonic()
        dropped: Counter[str] = Counter()
        sessions: list[str] = []
        state = {
            "seen": 0,
            "processed": 0,
            "truncated": 0,
            "turns": 0,
            "selected": 0,
        }
        discovery = "recent_contact"

        def report(outcome: str) -> ScanReport:
            return ScanReport(
                window_start=window_start,
                window_end=window_end,
                outcome=outcome,
                conversations_seen=state["seen"],
                conversations_processed=state["processed"],
                conversations_truncated=state["truncated"],
                turns_dispatched=state["turns"],
                entries_selected=state["selected"],
                entries_dropped=dict(dropped),
                sessions=tuple(sessions),
            )

        if not self._config.enabled:
            # 服务层兜底（AC29）：接线层本就不会在关时启动 Watcher
            return report("disabled")
        try:
            begun = self.scan_store.try_begin(window_start, window_end)
        except Exception as exc:  # noqa: BLE001 — store 失败不影响实时链路（§5.2 第 6 步）
            logger.warning("catchup try_begin failed: %s", type(exc).__name__)
            return report("store_blocked")
        if not begun:
            # 标记在扫描开始时落盘：已存在 = 该窗口扫过/在扫，零历史动作（AC5）
            return report("store_blocked")

        deadline = self._monotonic() + SCAN_TOTAL_TIMEOUT_SECONDS
        outcome = "completed"
        try:
            candidates, discovery = await self._discover(window_start, window_end, deadline)
            state["seen"] = len(candidates)
            if len(candidates) > MAX_CONVERSATIONS_PER_SCAN:
                state["truncated"] = len(candidates) - MAX_CONVERSATIONS_PER_SCAN
                candidates = candidates[:MAX_CONVERSATIONS_PER_SCAN]
            for kind, peer_id in candidates:
                if self._monotonic() >= deadline:
                    outcome = "timeout"
                    break
                try:
                    await self._process_conversation(
                        kind,
                        peer_id,
                        window_start,
                        window_end,
                        deadline,
                        dropped,
                        state,
                        sessions,
                        discovery,
                    )
                except _ScanDeadlineExceeded:
                    # 派发等待已耗尽整轮时限：以 timeout 收尾，本窗未处理
                    # 会话不再继续（窗口已在开始时落盘，宁漏勿重）。
                    outcome = "timeout"
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — 逐会话失败隔离、不重试（AC10）
                    dropped["conversation_error"] += 1
                    logger.warning(
                        "catchup conversation failed kind=%s: %s", kind, type(exc).__name__
                    )
                await self._sleep(0)  # 逐会话让行实时流量（AC26a 的结构基础）
        except asyncio.CancelledError:
            # 关停/取消：窗口已在开始时落盘，complete 为 timeout 后原样上抛
            self._complete(window_start, window_end, "timeout")
            raise
        self._complete(window_start, window_end, outcome)
        result = report(outcome)
        self._audit_scan(result, discovery, started)
        return result

    def _complete(self, window_start: float, window_end: float, outcome: str) -> None:
        try:
            self.scan_store.complete(window_start, window_end, outcome)
        except Exception as exc:  # noqa: BLE001 — complete 失败只记类型名（§5.2 第 6 步）
            logger.warning("catchup complete failed: %s", type(exc).__name__)

    # -- 发现与降级（§5.3） ---------------------------------------------------

    async def _discover(self, window_start: float, window_end: float, deadline: float):
        """返回 (有序候选 [(kind, peer_id)], discovery 标记)。"""
        contacts = None
        try:
            contacts = await self._history_call(self._history.recent_contacts(), deadline)
        except Exception as exc:  # noqa: BLE001 — 拉取失败按不可用处理，走降级
            logger.warning("catchup recent_contacts failed: %s", type(exc).__name__)
            contacts = None
        if contacts is not None:
            in_window = [
                c for c in contacts if window_start <= c.last_activity_at <= window_end
            ]
            in_window.sort(key=lambda c: c.last_activity_at, reverse=True)
            return [(c.kind, str(c.peer_id)) for c in in_window], "recent_contact"
        # 降级（OD7 钉死）：本地 messages 表私聊会话逐个试探；不能发现群会话
        try:
            user_ids = self.scan_store.known_private_user_ids(FALLBACK_MAX_CONVERSATIONS)
        except Exception as exc:  # noqa: BLE001 — 降级枚举失败即本轮无候选
            logger.warning("catchup fallback discovery failed: %s", type(exc).__name__)
            user_ids = []
        return [("private", str(uid)) for uid in user_ids], "fallback_private_only"

    async def _history_call(self, coro, deadline: float):
        """单次历史拉取：15s 上限，受扫描剩余时限钳制（§5.9）。"""
        budget = min(HISTORY_CALL_TIMEOUT_SECONDS, deadline - self._monotonic())
        if budget <= 0:
            # 与 _await_before_deadline 同口径：不再等待时先关闭协程对象，
            # 防未 await 警告与 GC 兜底（集成审查 P4 修复）。
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise TimeoutError("catchup scan deadline exceeded")
        return await asyncio.wait_for(coro, timeout=budget)

    async def _await_before_deadline(self, awaitable, deadline: float):
        """派发等待的剩余时限钳制（审查 P3 修复）：整轮 120s 总时限对
        派发回合的 await 同样生效，超时抛 ``_ScanDeadlineExceeded``。

        不用 shield：``wait_for`` 超时直接取消回合——与实时链的关停
        取消语义一致（``_run_event_safe`` 对 CancelledError 原样上抛）；
        shield 会让回合在扫描收尾、claim 已 finish(abandoned) 后仍在
        后台继续发送，竞态不可控。外层 CancelledError 不在此转换，
        仍按关停口径上抛。
        """
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            # 不再等待：协程对象直接关闭防未 await 警告；已建 task 则
            # 取消（与超时取消同一语义），不留后台孤儿回合。
            if isinstance(awaitable, asyncio.Task):
                awaitable.cancel()
            else:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
            raise _ScanDeadlineExceeded
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError as exc:
            raise _ScanDeadlineExceeded from exc

    # -- 逐会话处理 ------------------------------------------------------------

    async def _process_conversation(
        self, kind, peer_id, window_start, window_end, deadline, dropped, state, sessions,
        discovery,
    ) -> None:
        conv_started = self._monotonic()
        conv_dropped: Counter[str] = Counter()

        def audit(processed: bool, reason: str, extra: dict) -> None:
            base = {
                "window_start": window_start,
                "window_end": window_end,
                "session_hash": _session_hash(
                    f"g:{peer_id}" if kind == "group" else str(peer_id)
                ),
                "scene": "group" if kind == "group" else "private",
                "selected_count": 0,
                "dropped": dict(conv_dropped),
                "discovery": discovery,
                "truncated_count": 0,
            }
            base.update(
                {k: v for k, v in extra.items() if k in _AUDIT_EXTRA_KEYS}
            )
            self._log_conversation(processed, reason, conv_started, base)

        if kind == "group" and not getattr(self._processor, "group_reply_enabled", False):
            # 群回复产品开关关：整群跳过，不拉取后处理（§5.7 第 1 点）
            conv_dropped["group_reply_disabled"] += 1
            dropped.update(conv_dropped)
            audit(False, "group_reply_disabled", {})
            return
        try:
            if kind == "group":
                entries = await self._history_call(
                    self._history.group_history(peer_id), deadline
                )
            else:
                entries = await self._history_call(
                    self._history.friend_history(peer_id), deadline
                )
        except Exception as exc:  # noqa: BLE001 — 逐会话拉取失败跳过不重试（AC10）
            conv_dropped["history_error"] += 1
            dropped.update(conv_dropped)
            logger.warning("catchup history fetch failed: %s", type(exc).__name__)
            audit(False, "history_error", {})
            return
        if entries is None:
            conv_dropped["history_unavailable"] += 1
            dropped.update(conv_dropped)
            audit(False, "history_unavailable", {})
            return
        entries = sorted(entries, key=lambda e: e.sent_at)
        if kind == "group":
            await self._process_group(
                peer_id, entries, window_start, window_end, deadline,
                dropped, conv_dropped, state, sessions, audit,
            )
        else:
            await self._process_private(
                peer_id, entries, window_start, window_end, deadline,
                dropped, conv_dropped, state, sessions, audit,
            )

    # -- 逐条过滤与已处理核查（§5.4 / §5.5） ------------------------------------

    async def _filter_entry(
        self, entry, kind, window_start, window_end, dropped, *, check_processed=True
    ) -> bool:
        """§5.4 顺序钉死：任一命中即丢弃并计原因码。群专属过滤在 §5.7 侧。

        群路径按 §5.7 第 4 点的顺序：先 1–3 号过滤与触发判定，已处理
        核查只对触发条目做——故群侧首轮传 ``check_processed=False``。
        """
        event = entry.event
        if str(event.user_id) == str(event.self_id):
            dropped["self_sent"] += 1
            return False
        if entry.sent_at < window_start or entry.sent_at > window_end:
            dropped["outside_window"] += 1
            return False
        if not event.text.strip() and not event.images and not event.has_media:
            dropped["empty"] += 1
            return False
        if check_processed and await self._already_processed(entry, kind):
            dropped["already_processed"] += 1
            return False
        return True

    def _claim_key(self, entry, kind) -> tuple[str, str, str]:
        """claim 键口径（§5.5）：私聊三元组；群聊 message_id 分量加 ``g:`` 前缀。"""
        message_id = str(entry.message_id)
        if kind == "group":
            message_id = f"g:{message_id}"
        return (PLATFORM, str(entry.event.self_id), message_id)

    async def _already_processed(self, entry, kind) -> bool:
        """三层核查按序短路（§5.5）；群聊只有 claim 一层可用（如实标注）。"""
        if self._claim_store.get(self._claim_key(entry, kind)) is not None:
            return True
        if kind == "group":
            return False
        sender_id = str(entry.sender_id or entry.event.user_id)
        _had_send, ledger_state = await self._ledger.turn_had_send_with_state(
            sender_id, str(entry.message_id)
        )
        if ledger_state is not None:
            # 只看 state 存在性：该回合已被管线接管过，补回再发即重复
            return True
        return bool(self.scan_store.message_turn_exists(sender_id, str(entry.message_id)))

    # -- claim 协议（§5.6 步骤 1–2，私聊/群聊共用） -------------------------------

    def _claim_turn(self, turn_entry, kind, lane, text, dropped):
        """认领回合键。返回 (outcome, turn_key)；跳过时 outcome 为终局原因码。"""
        turn_key = self._claim_key(turn_entry, kind)
        if lane is None:  # 私聊：与 webhook 预筛分道同函数（§5.6 步骤 1）
            safety = getattr(self._processor, "safety", None)
            keyword_match = (
                safety.keyword_match if safety is not None else (lambda _text: None)
            )
            lane = classify_lane(text, keyword_match=keyword_match)
        outcome = self._claim_store.try_claim(turn_key, lane)
        if outcome is ClaimOutcome.DUPLICATE:
            dropped["claim_duplicate"] += 1
            return None, turn_key
        if outcome is ClaimOutcome.FULL:
            dropped["claim_full"] += 1
            return None, turn_key
        return outcome, turn_key  # CLAIMED / EXEMPT 均继续

    def _pre_claim_merged(self, merged, kind, lane, dropped):
        """被合并 id 逐个预登记终结（§5.6 步骤 2）；得 DUPLICATE / FULL
        者从合成输入剔除。"""
        kept = []
        for entry in merged:
            key = self._claim_key(entry, kind)
            outcome = self._claim_store.try_claim(key, lane)
            if outcome is ClaimOutcome.DUPLICATE:
                # 该条正被他方处理：剔除，避免同条被单条回合与合成回合各回一次
                dropped["already_processed"] += 1
                continue
            if outcome is ClaimOutcome.FULL:
                # 分区已满、无从为该 id 留去重痕迹（审查 P4 修复）：与
                # DUPLICATE 同处置剔除——继续合并且不留痕会使该 id 日后
                # 经事件通道重投时被当作新消息二次回复；宁漏勿重。
                dropped["claim_full"] += 1
                continue
            if outcome is ClaimOutcome.CLAIMED:
                self._claim_store.finish(key, "done")
            kept.append(entry)
        return kept

    def _finish_turn(self, turn_key, outcome, state_name: str) -> None:
        """收尾 finish：仅 CLAIMED 时执行（EXEMPT 已由 store 落终态，§5.6 步骤 1）。"""
        if outcome is ClaimOutcome.CLAIMED:
            self._claim_store.finish(turn_key, state_name)

    def _prefix(self) -> str | None:
        return self._config.prefix_text if self._config.prefix_enabled else None

    # -- 私聊合成与派发（§5.6） --------------------------------------------------

    async def _process_private(
        self, peer_id, entries, window_start, window_end, deadline, dropped,
        conv_dropped, state, sessions, audit,
    ) -> None:
        selected = []
        for entry in entries:
            if await self._filter_entry(entry, "private", window_start, window_end, conv_dropped):
                if str(entry.sender_id) != str(peer_id):
                    # 发送者与会话对端不一致（平台数据串话/方向误标）：
                    # 与适配层 no_sender 同口径 fail-closed 丢弃，防把他人
                    # 内容记到对端名下（集成审查 P4 修复）。
                    conv_dropped["sender_mismatch"] += 1
                    continue
                selected.append(entry)
        if not selected:
            dropped.update(conv_dropped)
            audit(False, "no_entries_selected", {})
            return
        if len(selected) > PER_CONVERSATION_LIMIT:
            conv_dropped["over_conversation_limit"] += len(selected) - PER_CONVERSATION_LIMIT
            selected = selected[-PER_CONVERSATION_LIMIT:]
        turn_entry = selected[-1]
        text = "\n".join(e.event.text for e in selected if e.event.text)
        outcome, turn_key = self._claim_turn(turn_entry, "private", None, text, conv_dropped)
        if outcome is None:
            dropped.update(conv_dropped)
            audit(False, "claim_duplicate" if "claim_duplicate" in conv_dropped else "claim_full",
                  {})
            return
        kept_merged = self._pre_claim_merged(selected[:-1], "private", LANE_ORDINARY, conv_dropped)
        kept = [*kept_merged, turn_entry]
        if not kept:  # 钉死守卫：剔除后输入为空则整会话跳过（§5.6 步骤 2 例外）
            self._finish_turn(turn_key, outcome, "abandoned")
            dropped.update(conv_dropped)
            audit(False, "all_merged_duplicate",
                  {})
            return
        text = "\n".join(e.event.text for e in kept if e.event.text)
        parsed = UserMessage(
            user_id=str(peer_id),
            text=text,
            message_id=str(turn_entry.message_id),
            has_media=any(e.event.has_media for e in kept),
            images=tuple(img for e in kept for img in e.event.images),
        )
        extra = {
            "selected_count": len(kept),
        }
        try:
            task = await self._await_before_deadline(
                self._processor.dispatch_parsed(
                    parsed, catchup_prefix=self._prefix()
                ),
                deadline,
            )
        except _ScanDeadlineExceeded:
            self._finish_turn(turn_key, outcome, "abandoned")
            conv_dropped["dispatch_timeout"] += 1
            dropped.update(conv_dropped)
            audit(False, "dispatch_timeout", extra)
            raise
        except Exception as exc:  # noqa: BLE001 — 派发失败按会话隔离
            self._finish_turn(turn_key, outcome, "abandoned")
            conv_dropped["dispatch_failed"] += 1
            dropped.update(conv_dropped)
            logger.warning("catchup dispatch failed: %s", type(exc).__name__)
            audit(False, "dispatch_failed", extra)
            return
        if task is None:  # 代次推进失败（§5.6 步骤 4）
            self._finish_turn(turn_key, outcome, "abandoned")
            conv_dropped["dispatch_failed"] += 1
            dropped.update(conv_dropped)
            audit(False, "dispatch_failed", extra)
            return
        try:
            await self._await_before_deadline(task, deadline)
        except _ScanDeadlineExceeded:
            # wait_for 已取消回合 task（不 shield，理由见 _await_before_deadline）
            self._finish_turn(turn_key, outcome, "abandoned")
            conv_dropped["dispatch_timeout"] += 1
            dropped.update(conv_dropped)
            audit(False, "dispatch_timeout", extra)
            raise
        except asyncio.CancelledError:
            self._finish_turn(turn_key, outcome, "abandoned")
            raise
        except Exception as exc:  # noqa: BLE001 — 回合异常按会话隔离
            self._finish_turn(turn_key, outcome, "abandoned")
            conv_dropped["dispatch_failed"] += 1
            dropped.update(conv_dropped)
            logger.warning("catchup turn failed: %s", type(exc).__name__)
            audit(False, "dispatch_failed", extra)
            return
        self._finish_turn(turn_key, outcome, "done")
        state["processed"] += 1
        state["turns"] += 1
        state["selected"] += len(kept)
        sessions.append(str(peer_id))
        dropped.update(conv_dropped)
        audit(True, "dispatched", extra)

    # -- 群聊合成与派发（§5.7） --------------------------------------------------

    async def _process_group(
        self, peer_id, entries, window_start, window_end, deadline, dropped,
        conv_dropped, state, sessions, audit,
    ) -> None:
        candidates = []
        for entry in entries:
            if getattr(entry.event, "sub_type", None) in ("anonymous", "notice"):
                # 与事件道口径对齐：匿名/通知类群事件无稳定身份，实时通道
                # 只登记不入群子链；历史通道同口径剔除（集成审查 P3 修复）。
                conv_dropped["group_anonymous"] += 1
                continue
            if str(getattr(entry.event, "group_id", "") or "") != str(peer_id):
                # 与私聊 sender_mismatch 对称：条目自带 group_id 与被扫
                # 群不一致（平台串话/方向错标）时 fail-closed 丢弃，防
                # 异群内容并入本群回合、回复被发往异群（复审轮 2 P3 修复）。
                conv_dropped["group_id_mismatch"] += 1
                continue
            if await self._filter_entry(
                entry, "group", window_start, window_end, conv_dropped,
                check_processed=False,
            ):
                candidates.append(entry)
        if not candidates:
            dropped.update(conv_dropped)
            audit(False, "no_entries_selected", {})
            return
        if any(not entry.at_known for entry in candidates):
            # @ 信息还原不了 → 整群跳过（AC18，宁漏勿扰，§5.7 第 2 点）
            conv_dropped["group_at_unknown"] += len(candidates)
            dropped.update(conv_dropped)
            audit(False, "group_at_unknown", {})
            return
        triggered = []
        for entry in candidates:
            if is_group_trigger(entry.event):  # 直接调用既有判定，不复刻（AC17）
                triggered.append(entry)
            else:
                conv_dropped["group_not_trigger"] += 1
        if not triggered:
            dropped.update(conv_dropped)
            audit(False, "group_not_trigger", {})
            return
        # §5.7 第 4 点：在触发条目内做 §5.5 claim 核查（群聊只有 claim 一层）
        survivors = []
        for entry in triggered:
            if await self._already_processed(entry, "group"):
                conv_dropped["already_processed"] += 1
            else:
                survivors.append(entry)
        triggered = survivors
        if not triggered:
            dropped.update(conv_dropped)
            audit(False, "no_entries_selected", {})
            return
        # 逐条主体准入（集成审查 P3 修复）：合成回合只按最新条目身份过
        # handle_group_message 的双维准入门，会让被拦截/未准入发送者的
        # 内容搭最新条目的身份被回复——与事件道逐条口径对齐，合并前先
        # 按同一谓词逐条过滤（主体计算与准入判定均复用 events 既有函数，
        # 名单取 processor 当前接线值，与下方派发调用逐字一致）。
        processor = self._processor
        admitted = []
        for entry in triggered:
            subject = group_subject(
                entry.event, allowed_ids=getattr(processor, "allowed", ())
            )
            if group_reply_subject_admitted(
                subject,
                blocked_persons=processor.group_reply_blocked_persons,
                allowed_persons=processor.group_reply_allowed_persons,
                allowed_roles=processor.group_reply_allowed_roles,
            ):
                admitted.append(entry)
            else:
                conv_dropped["group_not_admitted"] += 1
        triggered = admitted
        if not triggered:
            dropped.update(conv_dropped)
            audit(False, "no_entries_selected", {})
            return
        if len(triggered) > PER_CONVERSATION_LIMIT:
            conv_dropped["over_conversation_limit"] += len(triggered) - PER_CONVERSATION_LIMIT
            triggered = triggered[-PER_CONVERSATION_LIMIT:]
        turn_entry = triggered[-1]
        outcome, turn_key = self._claim_turn(
            turn_entry, "group", LANE_GROUP, "", conv_dropped
        )
        if outcome is None:
            dropped.update(conv_dropped)
            audit(False, "claim_duplicate" if "claim_duplicate" in conv_dropped else "claim_full",
                  {})
            return
        kept_merged = self._pre_claim_merged(triggered[:-1], "group", LANE_GROUP, conv_dropped)
        kept = [*kept_merged, turn_entry]
        if not kept:
            self._finish_turn(turn_key, outcome, "abandoned")
            dropped.update(conv_dropped)
            audit(False, "all_merged_duplicate",
                  {})
            return
        synthetic = dataclasses.replace(
            turn_entry.event,
            segments=tuple(seg for e in kept for seg in e.event.segments),
            text="\n".join(e.event.text for e in kept if e.event.text),
            message_id=str(turn_entry.message_id),
            images=tuple(img for e in kept for img in e.event.images),
        )
        extra = {
            "selected_count": len(kept),
        }
        processor = self._processor
        try:
            # 与事件道消费者逐字同形（§5.7 第 6 点，对照 ingress/__init__ 消费段）；
            # 派发等待同样受整轮剩余时限钳制（审查 P3 修复，同私聊口径）
            await self._await_before_deadline(
                handle_group_message(
                    processor,
                    synthetic,
                    responder=_wrap_group_responder(
                        processor.group_responder, self._prefix()
                    )
                    if processor.group_reply_enabled
                    else None,
                    blocked_persons=processor.group_reply_blocked_persons,
                    allowed_persons=processor.group_reply_allowed_persons,
                    allowed_roles=processor.group_reply_allowed_roles,
                ),
                deadline,
            )
        except _ScanDeadlineExceeded:
            self._finish_turn(turn_key, outcome, "abandoned")
            conv_dropped["group_dispatch_timeout"] += 1
            dropped.update(conv_dropped)
            audit(False, "group_dispatch_timeout", extra)
            raise
        except Exception as exc:  # noqa: BLE001 — 对照消费者段的 abandoned 收尾
            self._finish_turn(turn_key, outcome, "abandoned")
            conv_dropped["group_dispatch_error"] += 1
            dropped.update(conv_dropped)
            logger.warning("catchup group dispatch failed: %s", type(exc).__name__)
            audit(False, "group_dispatch_error", extra)
            return
        self._finish_turn(turn_key, outcome, "done")
        state["processed"] += 1
        state["turns"] += 1
        state["selected"] += len(kept)
        sessions.append(str(peer_id))
        dropped.update(conv_dropped)
        audit(True, "dispatched", extra)

    # -- 审计（§5.8：只落白名单键，零正文） ---------------------------------------

    def _log_conversation(self, processed, reason, started, extra) -> None:
        log = getattr(self._processor, "_log", None)
        if not callable(log):
            return
        elapsed_ms = (self._monotonic() - started) * 1000.0
        try:
            log("catchup_conversation", 0, processed, elapsed_ms,
                reason_code=reason, extra=extra)
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响扫描
            logger.warning("catchup conversation audit failed: %s", type(exc).__name__)

    def _audit_scan(self, result: ScanReport, discovery: str, started: float) -> None:
        log = getattr(self._processor, "_log", None)
        if not callable(log):
            return
        extra = {
            "window_start": result.window_start,
            "window_end": result.window_end,
            "selected_count": result.entries_selected,
            "dropped": dict(result.entries_dropped),
            "discovery": discovery,
            "truncated_count": result.conversations_truncated,
        }
        elapsed_ms = (self._monotonic() - started) * 1000.0
        try:
            log("catchup_scan", 0, result.outcome == "completed", elapsed_ms,
                reason_code=result.outcome, extra=extra)
        except Exception as exc:  # noqa: BLE001 — 审计失败不影响扫描结果
            logger.warning("catchup scan audit failed: %s", type(exc).__name__)
