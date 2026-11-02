"""P3 saturation / budget tests for the pipeline acceptance boundary (§5.3).

Contract under test (coordinator-fixed for P3):

- ``app.state.claim_store`` is a partitioned claim store (P5 起为落库版
  ``SqliteClaimStore``，公开 API 与分区语义与进程内 ``ClaimStore`` 一致；partitions
  {"ordinary", "reserved"}); ordinary/control traffic claim the ordinary /
  reserved partition by lane, a keyword crisis claims the reserved partition,
  and a full reserved partition still admits a crisis via an exempted claim
  (``exempted_count`` +1; the persistent store additionally leaves a
  terminal dedup record in its separate "exempt" partition — not counted
  against any capacity — so a re-delivery of the same crisis is answered
  as a duplicate instead of being sent again; Codex catch-up review fix).
- ``app.state.event_bus`` exposes three bounded lanes — ``queue`` (ordinary,
  default 200), ``control_queue`` (50), ``critical_queue`` (20) — whose
  capacities (and the claim capacities / judge wait) are overridable through
  ``settings.app["pipeline"]``.
- The webhook is the single acceptance boundary: a full ordinary/control
  queue, or a full ordinary claim partition, is refused with 503 *before* a
  claim is created; a duplicate claim answers 204 without re-queueing; a
  crisis is still accepted (204) through the critical lane while ordinary is
  saturated; when even the critical queue is full, the minimal crisis reply
  (unnotified variant) is sent synchronously inside the request, then 204.
- Processing budgets: ordinary/control share a worker pool of 8 and critical
  has a dedicated pool of 2 (max in-flight sends therefore ≤ 10); the judge
  semaphore is 2 and waiting past ``processor._judge_wait_seconds`` (default
  5s) is treated as suspected crisis.

Everything runs against fake senders / fake judges only. No consumer is
started in the pure saturation cases, so queued events are never processed
and the assertions observe the acceptance boundary alone; cases that start
the consumer poll on the recorded sends instead of joining lane queues, so
they do not depend on any one lane's task accounting.
"""

import asyncio
import time
from dataclasses import replace

import pytest
import yaml

from companion.brain import CompanionBrain
from companion.config import load_settings
from companion.ingress import create_app
from companion.memory import SqliteClaimStore
from companion.safety import ModelLLMJudge, crisis_reply
from tests import test_app
from tests.fake_onebot import private_event
from tests.test_adapter_contract_p0 import _post, fake_event, signed_step

SECRET = test_app.SECRET
USER = "10001"
FORGET_REPLY = "好的，我已删除保存的短期记忆。"
NORMAL_REPLY = "测试回复"


# ---------------------------------------------------------------------------
# App builder (same fixture shape as tests.test_app.setup_app, plus the
# pipeline capacity overrides and an injectable recording sender)
# ---------------------------------------------------------------------------


class FakeSender:
    """Instant sender recording every outbound message (setup_app shape)."""

    def __init__(self):
        self.calls = []

    async def send_private_msg(self, user_id, message, *, priority="normal", **_kwargs):
        self.calls.append({"user_id": int(user_id), "message": message})
        return {"status": "ok"}

    async def send_alert_private_msg(self, user_id, message):
        return await self.send_private_msg(user_id, message)


class TrackingSender(FakeSender):
    """Sender recording the peak number of simultaneously in-flight sends."""

    def __init__(self, delay_seconds: float = 0.1):
        super().__init__()
        self.delay_seconds = delay_seconds
        self.in_flight = 0
        self.max_in_flight = 0

    async def send_private_msg(self, user_id, message, *, priority="normal", **_kwargs):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay_seconds)
            self.calls.append({"user_id": int(user_id), "message": message})
            return {"status": "ok"}
        finally:
            self.in_flight -= 1


class FakeAlerter:
    def __init__(self):
        self.items = []

    async def alert(self, user_id, reason, summary="sensitive text omitted"):
        self.items.append((user_id, reason, summary))


def build_app(tmp_path, monkeypatch, *, pipeline=None, sender=None):
    """Build a fresh app over tmp settings; ``pipeline`` goes to app["pipeline"]."""
    monkeypatch.setenv("ONEBOT_SECRET", SECRET)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_QQ", raising=False)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    base = load_settings()
    app_settings = {
        **base.app,
        "allowed_private_user_ids": ["10001"],
        "sqlite_path": "memory.db",
        "logs_dir": "logs",
        "response_delay_seconds": {"min": 0, "max": 0},
        "requests_per_minute": 6,
    }
    if pipeline is not None:
        app_settings["pipeline"] = pipeline
    settings = replace(
        base,
        onebot_secret=SECRET,
        dev=False,
        root=tmp_path,
        app=app_settings,
        safety={
            **base.safety,
            "helplines": ["12356", "110", "120"],
            "crisis_keywords": ["活着没意思", "想自杀", "准备好了"],
            "categories": [
                {"name": "低落", "severity": 1, "keywords": ["活着没意思"]},
                {"name": "明确自伤计划", "severity": 3, "keywords": ["想自杀", "准备好了"]},
            ],
            "crisis_copy": {
                "notify_enabled": "已经通知了真人。",
                "notify_disabled": "我是 AI，没办法替你联系别人。",
            },
        },
    )

    class FakeLLM:
        async def generate(self, _prompt):
            return NORMAL_REPLY

        async def generate_json(self, _prompt):
            return '{"bubbles": ["测试回复"]}'

    class SafeJudgeLLM:
        async def generate(self, _prompt):
            return "NO"

    async def no_sleep(_delay):
        return None

    sender = sender if sender is not None else FakeSender()
    app = create_app(
        settings,
        sender=sender,
        alerter=FakeAlerter(),
        sleep=no_sleep,
        random_uniform=lambda _a, _b: 0,
        brain=CompanionBrain(settings.persona, FakeLLM()),
    )
    # Same judge substitution as setup_app: a keyless fallback LLM must not
    # turn every ordinary message into a suspected crisis in these tests.
    app.state.processor.safety.judge = ModelLLMJudge(SafeJudgeLLM())
    return app, sender, settings


def expected_crisis(settings) -> str:
    """The unnotified fixed crisis copy (notify=False variant)."""
    return crisis_reply(settings.safety["helplines"], False, settings.safety)


def messages(sender) -> list[str]:
    return [call["message"] for call in sender.calls]


# ---------------------------------------------------------------------------
# Posting / consumer / polling helpers
# ---------------------------------------------------------------------------


async def post_status(app, event) -> int:
    body, headers = signed_step(event)
    response = await _post(app, body, headers)
    return response.status_code


def post_statuses(app, events) -> list[int]:
    """Post events sequentially with no consumer running."""

    async def scenario():
        return [await post_status(app, event) for event in events]

    return asyncio.run(scenario())


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def run_with_consumer(app, scenario):
    """Run ``scenario()`` with the event consumer task live, then wind down."""

    async def wrapper():
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            return await scenario()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            try:
                await asyncio.wait_for(app.state.processor.wait_idle(), timeout=5)
            except Exception:
                pass

    return asyncio.run(wrapper())


# ---------------------------------------------------------------------------
# Claim lookup helpers (claim key is the webhook's (platform, self_id,
# message_id) tuple; the bare message_id probe is only a lookup fallback)
# ---------------------------------------------------------------------------


def claim_key(mid: str):
    return ("onebot", "900000001", mid)


def get_claim(app, mid: str):
    store = app.state.claim_store
    for key in (claim_key(mid), mid):
        try:
            record = store.get(key)
        except Exception:
            record = None
        if record is not None:
            return record
    return None


def claim_partition(record) -> str | None:
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get("partition") or record.get("lane")
    return getattr(record, "partition", None) or getattr(record, "lane", None)


def exempted_count(app) -> int:
    value = getattr(app.state.claim_store, "exempted_count")
    return int(value() if callable(value) else value)


def mark_onboarded(app, *user_ids: str) -> None:
    for user_id in user_ids:
        asyncio.run(app.state.processor.conversation.mark_onboarded(str(user_id)))


# ---------------------------------------------------------------------------
# Structure: claim store, dedicated critical pool, three bounded lanes
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_pipeline_structure_defaults(tmp_path, monkeypatch):
    app, _sender, _settings = build_app(tmp_path, monkeypatch)
    store = app.state.claim_store
    assert isinstance(store, SqliteClaimStore)
    assert set(store.partitions) == {"ordinary", "reserved"}
    for method in ("try_claim", "finish", "release", "get", "counts"):
        assert callable(getattr(store, method)), method
    assert exempted_count(app) == 0

    executor = app.state.critical_executor
    assert executor is not None  # critical has a dedicated execution pool
    pool_size = next(
        (
            getattr(executor, name)
            for name in ("pool_size", "_max_workers", "max_workers")
            if isinstance(getattr(executor, name, None), int)
        ),
        None,
    )
    assert pool_size == 2, "critical_executor must expose pool_size=2"

    bus = app.state.event_bus
    assert bus.queue.maxsize == 200
    assert bus.control_queue.maxsize == 50
    assert bus.critical_queue.maxsize == 20
    assert app.state.processor._judge_wait_seconds == 5


@pytest.mark.timeout(10)
def test_pipeline_settings_override_capacities(tmp_path, monkeypatch):
    pipeline = {
        "ordinary_queue": 3,
        "control_queue": 7,
        "critical_queue": 11,
        "ordinary_claim_capacity": 4,
        "reserved_claim_capacity": 5,
        "judge_wait_seconds": 1.5,
    }
    app, _sender, _settings = build_app(tmp_path, monkeypatch, pipeline=pipeline)
    bus = app.state.event_bus
    assert bus.queue.maxsize == 3
    assert bus.control_queue.maxsize == 7
    assert bus.critical_queue.maxsize == 11
    # Claim capacities are asserted behaviourally in the saturation cases
    # below; the judge wait is a plain processor attribute per the contract.
    assert app.state.processor._judge_wait_seconds == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Acceptance boundary: ordinary lane saturation (queue and claim partition)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_ordinary_lane_saturation_returns_503_and_claims_nothing(tmp_path, monkeypatch):
    app, _sender, _settings = build_app(
        tmp_path, monkeypatch, pipeline={"ordinary_queue": 3}
    )
    events = [
        fake_event(f"sat-ord-{index}", user=USER, text=f"普通消息{index}")
        for index in range(4)
    ]
    statuses = post_statuses(app, events)  # consumer deliberately not started
    assert statuses == [204, 204, 204, 503]
    for index in range(3):
        assert get_claim(app, f"sat-ord-{index}") is not None
    # The refused event was rejected before claim creation: no claim record.
    assert get_claim(app, "sat-ord-3") is None


@pytest.mark.timeout(10)
def test_ordinary_claim_partition_full_returns_503_before_claim(tmp_path, monkeypatch):
    app, _sender, _settings = build_app(
        tmp_path,
        monkeypatch,
        pipeline={"ordinary_queue": 10, "ordinary_claim_capacity": 1},
    )
    events = [
        fake_event("sat-claim-1", user=USER, text="普通消息一"),
        fake_event("sat-claim-2", user=USER, text="普通消息二"),
    ]
    statuses = post_statuses(app, events)
    assert statuses == [204, 503]  # queue has room; the claim partition is full
    first = get_claim(app, "sat-claim-1")
    assert first is not None and claim_partition(first) == "ordinary"
    assert get_claim(app, "sat-claim-2") is None


# ---------------------------------------------------------------------------
# Control lane: not starved by ordinary saturation, bounded on its own lane
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_control_lane_not_blocked_by_ordinary_saturation(tmp_path, monkeypatch):
    app, sender, _settings = build_app(
        tmp_path, monkeypatch, pipeline={"ordinary_queue": 1}
    )
    mark_onboarded(app, USER)  # no onboarding send may precede the assertion
    statuses = post_statuses(
        app,
        [
            fake_event("sat-ctl-fill", user=USER, text="普通消息"),
            fake_event("sat-ctl-forget", user=USER, text="忘掉我"),
        ],
    )
    assert statuses == [204, 204]  # forget goes to the control lane, not 503

    async def scenario():
        assert await wait_until(lambda: FORGET_REPLY in messages(sender))
        return list(sender.calls)

    calls = run_with_consumer(app, scenario)
    # Control is dequeued ahead of the queued ordinary event: the very first
    # outbound reply is the forget confirmation copy.
    assert calls[0]["message"] == FORGET_REPLY


@pytest.mark.timeout(10)
def test_control_queue_full_returns_503_before_claim(tmp_path, monkeypatch):
    app, _sender, _settings = build_app(
        tmp_path, monkeypatch, pipeline={"control_queue": 1}
    )
    statuses = post_statuses(
        app,
        [
            fake_event("sat-ctlq-1", user=USER, text="忘掉我"),
            fake_event("sat-ctlq-2", user=USER, text="忘掉我"),
        ],
    )
    assert statuses == [204, 503]
    assert get_claim(app, "sat-ctlq-2") is None


# ---------------------------------------------------------------------------
# Critical lane: reserved-partition保底, exemption, synchronous fallback
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_critical_claim_uses_reserved_partition_under_saturation(tmp_path, monkeypatch):
    app, sender, settings = build_app(
        tmp_path,
        monkeypatch,
        pipeline={"ordinary_queue": 1, "ordinary_claim_capacity": 1},
    )
    mark_onboarded(app, USER)
    statuses = post_statuses(
        app,
        [
            fake_event("sat-crit-fill", user=USER, text="普通消息"),
            fake_event("sat-crit-1", user=USER, text="想自杀"),
        ],
    )
    # Ordinary queue *and* ordinary claim partition are both full, yet the
    # keyword crisis is still accepted through the critical lane.
    assert statuses == [204, 204]
    record = get_claim(app, "sat-crit-1")
    assert record is not None and claim_partition(record) == "reserved"

    async def scenario():
        return await wait_until(lambda: expected_crisis(settings) in messages(sender))

    assert run_with_consumer(app, scenario), "crisis reply was not delivered"


@pytest.mark.timeout(10)
def test_crisis_exemption_when_reserved_partition_full(tmp_path, monkeypatch):
    app, sender, settings = build_app(
        tmp_path, monkeypatch, pipeline={"reserved_claim_capacity": 0}
    )
    mark_onboarded(app, USER)
    statuses = post_statuses(app, [fake_event("sat-exempt-1", user=USER, text="想自杀")])
    assert statuses == [204]
    # Reserved partition cannot hold the claim: the crisis is exempted —
    # counted for audit — and still enqueued critical. The persistent
    # store leaves a terminal dedup record in the separate "exempt"
    # partition (Codex catch-up review fix), so re-posting the same
    # crisis is a duplicate, not a second exemption or a second send.
    assert exempted_count(app) == 1
    record = get_claim(app, "sat-exempt-1")
    assert record is not None and claim_partition(record) == "exempt"
    assert post_statuses(app, [fake_event("sat-exempt-1", user=USER, text="想自杀")]) == [204]
    assert exempted_count(app) == 1

    async def scenario():
        return await wait_until(lambda: expected_crisis(settings) in messages(sender))

    assert run_with_consumer(app, scenario), "exempted crisis was not delivered"


@pytest.mark.timeout(10)
def test_critical_queue_full_sends_synchronous_minimal_crisis(tmp_path, monkeypatch):
    app, sender, settings = build_app(
        tmp_path, monkeypatch, pipeline={"critical_queue": 1}
    )
    mark_onboarded(app, USER)
    first = fake_event("sat-sync-1", user=USER, text="想自杀")
    second = fake_event("sat-sync-2", user=USER, text="想自杀")
    # Consumer deliberately not started: the first crisis only queues up.
    assert post_statuses(app, [first]) == [204]
    assert sender.calls == []
    # The critical queue is now full, so the second crisis cannot queue: the
    # acceptance boundary itself synchronously sends the minimal crisis
    # reply (unnotified variant) inside the request, then answers 204.
    assert post_statuses(app, [second]) == [204]
    assert messages(sender) == [expected_crisis(settings)]


# ---------------------------------------------------------------------------
# Processing budgets: worker pools (8 ordinary/control + 2 critical)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(20)
def test_worker_pool_concurrency_budget(tmp_path, monkeypatch):
    sender = TrackingSender(delay_seconds=0.1)
    app, sender, settings = build_app(tmp_path, monkeypatch, sender=sender)
    ordinary_users = [f"320{index:02d}" for index in range(1, 13)]
    crisis_users = [f"330{index:02d}" for index in range(1, 4)]
    mark_onboarded(app, *ordinary_users, *crisis_users)
    events = [
        fake_event(f"sat-pool-ord-{index}", user=user, text=f"普通闲聊{index}")
        for index, user in enumerate(ordinary_users, start=1)
    ] + [
        fake_event(f"sat-pool-crit-{index}", user=user, text="想自杀")
        for index, user in enumerate(crisis_users, start=1)
    ]

    async def scenario():
        statuses = await asyncio.gather(*(post_status(app, event) for event in events))
        assert list(statuses) == [204] * len(events)
        completed = await wait_until(lambda: len(sender.calls) >= 15, timeout=8)
        return completed, list(sender.calls), sender.max_in_flight

    completed, calls, max_in_flight = run_with_consumer(app, scenario)
    assert completed and len(calls) == 15
    # 8 shared ordinary/control workers + 2 critical workers is the ceiling.
    assert max_in_flight <= 10
    crisis = expected_crisis(settings)
    by_user: dict[int, list[str]] = {}
    for call in calls:
        by_user.setdefault(call["user_id"], []).append(call["message"])
    for user in crisis_users:
        assert by_user[int(user)] == [crisis]
    for user in ordinary_users:
        assert by_user[int(user)] == [NORMAL_REPLY]


# ---------------------------------------------------------------------------
# Processing budgets: judge semaphore (2) + wait deadline
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_judge_wait_timeout_is_suspected_crisis(tmp_path, monkeypatch):
    app, sender, settings = build_app(tmp_path, monkeypatch)
    processor = app.state.processor
    processor._judge_wait_seconds = 0.2

    class BlockingJudge:
        """Judge occupying a semaphore slot for ~1s, then ruling 'no crisis'."""

        async def judge(self, _text):
            await asyncio.sleep(1.0)
            return False

    processor.safety.judge = BlockingJudge()
    users = ["34001", "34002", "34003"]
    mark_onboarded(app, *users)

    async def scenario():
        return await asyncio.gather(
            *(
                processor.handle(private_event(user, f"普通闲聊{index}"))
                for index, user in enumerate(users)
            )
        )

    results = asyncio.run(scenario())
    assert all(results)  # every user got exactly one outbound reply
    crisis = expected_crisis(settings)
    by_user: dict[int, list[str]] = {}
    for call in sender.calls:
        by_user.setdefault(call["user_id"], []).append(call["message"])
    assert sorted(by_user) == [34001, 34002, 34003]
    # Two judge slots are held for ~1s; the third caller waits past the 0.2s
    # deadline and is handled as suspected crisis (fixed crisis copy), while
    # the two slot holders get the judge's 'no crisis' → normal reply.
    crisis_users = [user for user, msgs in by_user.items() if msgs == [crisis]]
    normal_users = [user for user, msgs in by_user.items() if msgs == [NORMAL_REPLY]]
    assert len(crisis_users) == 1
    assert len(normal_users) == 2
