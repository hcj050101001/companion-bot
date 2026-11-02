"""P0 adapter contract tests for the v4 TransportGate (plan §5.2/§5.3).

The v4 ingress contract was revised in collision round 3 without a further
Codex review (plan §11); these cases are the P0 proof obligation for it.
Clauses the current code already satisfies are plain tests. Clauses it
cannot satisfy are registered known differences, one ``KD-Cxx`` id each,
expressed as ``xfail(strict=True)`` so a later stage fixing them turns the
case into a hard failure until the registration is retired:

- KD-C01 missing message_id still processed (fixed in P2: key extraction)
- KD-C02 self_id never read or required (fixed in P2)
- KD-C03 dedup key is message_id alone, not (platform, self_id, message_id)
  (fixed in P2; claim persistence was KD-C08, fixed in P5)
- KD-C04 ineligible/malformed events claim message_id before qualification (fixed in P2)
- KD-C05 Ignore carries no reason_code or event-type summary (fixed in P2)
- KD-C06 malformed bodies return 400 instead of the Ignore path (fixed in P2)
- KD-C07 missing user_id is not rejected at normalize (fixed in P2)
- KD-C08 dedup/claim state is process-local and unbounded, lost on restart
  (fixed in P5: claim 落库持久化)
- KD-C09 no per-lane partitioned claim capacity with a critical/control reserve (fix: P3)
- KD-C10 no bounded lanes / capacity reservation / 503 single acceptance boundary (fix: P3)
- KD-C11 no claim exemption or dedicated pool for keyword-crisis events (fix: P3)

P3 disposition: KD-C09/KD-C10/KD-C11 were fixed in P3, so their cases below
run as plain regression tests (xfail retired in the registry). KD-C08 was
partially closed in P3 — only its persistence gap remained, registered for
P5. P5 disposition: that gap is now closed (claim 落库，SqliteClaimStore 与
processor 共用同一 Database)，其两条用例同样转为普通回归测试（xfail 退役）。
"""

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from companion.brain import CompanionBrain
from companion.ingress import create_app
from companion.safety import ModelLLMJudge
from tests import test_app
from tests.fake_onebot import private_event

setup_app = test_app.setup_app
SECRET = test_app.SECRET


def signed(body: bytes, secret: str = SECRET) -> str:
    return "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def encode(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False).encode()


def fake_event(mid: str, user: str = "90001", text: str = "你好，今天有点累",
               self_id: str | None = "900000001") -> dict:
    event = private_event(user, text)
    event["message_id"] = mid
    if self_id is not None:
        event["self_id"] = self_id
    else:
        # private_event 自带 self_id（与真实报文对齐）；本 helper 的
        # self_id=None 语义是「报文缺 self_id」，须显式删除。
        event.pop("self_id", None)
    return event


async def _post(app, body: bytes, headers: dict | None = None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/onebot/event", content=body, headers=headers or {})


def run_posts(app, steps):
    """Run (body, headers) posts sequentially, draining the worker each time."""

    async def scenario():
        statuses = []
        for body, headers in steps:
            response = await _post(app, body, headers)
            statuses.append(response.status_code)
            await test_app.drain(app)
        return statuses

    return asyncio.run(test_app.request_with_worker(app, scenario))


def signed_step(event: dict):
    body = encode(event) if isinstance(event, dict) else event
    return body, {"X-Signature": signed(body)}


# ---------------------------------------------------------------------------
# Clauses the current ingress already satisfies
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_missing_signature_returns_401_and_leaves_no_residue(setup_app):
    app, calls, _, _ = setup_app
    body = encode(fake_event("contract-nosig-1"))
    statuses = run_posts(app, [(body, {}), signed_step(fake_event("contract-nosig-1"))])
    assert statuses == [401, 204]
    # The rejected attempt must not claim the id: the signed retry is processed.
    assert [call["message"] for call in calls].count("测试回复") == 1


@pytest.mark.timeout(10)
def test_wrong_signature_returns_401_and_sends_nothing(setup_app):
    app, calls, _, _ = setup_app
    body = encode(fake_event("contract-badsig-1"))
    statuses = run_posts(app, [(body, {"X-Signature": "sha1=bad"})])
    assert statuses == [401]
    assert calls == []


@pytest.mark.timeout(10)
def test_signature_is_checked_before_body_parsing(setup_app):
    app, calls, _, _ = setup_app
    # v4 single acceptance boundary step 1: signature failure wins over a
    # malformed body; the request never reaches parsing or the queue.
    statuses = run_posts(app, [(b"{not json", {"X-Signature": "sha1=bad"})])
    assert statuses == [401]
    assert calls == []


@pytest.mark.timeout(10)
def test_duplicate_message_id_is_processed_exactly_once(setup_app):
    app, calls, _, _ = setup_app
    step = signed_step(fake_event("contract-dup-1"))
    statuses = run_posts(app, [step, step])
    assert statuses == [204, 204]  # duplicate claim still answers 204 (accepted)
    assert [call["message"] for call in calls].count("测试回复") == 1


@pytest.mark.parametrize(
    "event",
    [
        {"post_type": "message", "message_type": "group", "group_id": 80001,
         "user_id": "90001", "message_id": "contract-nm-g", "message": "群里的话",
         "raw_message": "群里的话"},
        {"post_type": "notice", "notice_type": "friend_add", "user_id": "90001"},
        {"post_type": "request", "request_type": "friend", "user_id": "90001",
         "comment": "fake", "flag": "fake-flag"},
        {"post_type": "meta_event", "meta_event_type": "heartbeat",
         "status": {"online": True, "good": True}, "interval": 5000},
        {"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect"},
    ],
    ids=["group", "notice", "request", "heartbeat", "lifecycle"],
)
@pytest.mark.timeout(10)
def test_non_message_events_return_204_and_send_nothing(setup_app, event):
    app, calls, _, _ = setup_app
    statuses = run_posts(app, [signed_step(event)])
    assert statuses == [204]
    assert calls == []


@pytest.mark.timeout(10)
def test_malformed_body_sends_nothing_and_does_not_block_later_events(setup_app):
    app, calls, _, _ = setup_app
    bad = b"{not json"
    statuses = run_posts(
        app,
        [(bad, {"X-Signature": signed(bad)}), signed_step(fake_event("contract-after-bad"))],
    )
    assert statuses[1] == 204
    assert [call["message"] for call in calls].count("测试回复") == 1


@pytest.mark.timeout(10)
def test_keyless_meta_events_leave_no_dedup_residue(setup_app):
    app, calls, _, _ = setup_app
    heartbeat = {"post_type": "meta_event", "meta_event_type": "heartbeat",
                 "status": {"online": True, "good": True}, "interval": 5000}
    step = signed_step(heartbeat)
    statuses = run_posts(app, [step, step, step, signed_step(fake_event("contract-after-meta"))])
    assert statuses == [204, 204, 204, 204]
    assert [call["message"] for call in calls].count("测试回复") == 1


@pytest.mark.timeout(10)
def test_webhook_returns_204_before_processing_completes(setup_app):
    app, calls, _, _ = setup_app

    class BlockingJudge:
        """Judge stub blocked on an event until the test releases it.

        Event-based (no wall-clock threshold): if the HTTP response arrives
        while the judge is still blocked, acceptance is provably decoupled
        from processing, regardless of machine load.
        """

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, _prompt):
            self.started.set()
            await self.release.wait()
            return "NO"

    judge = BlockingJudge()
    app.state.processor.safety.judge.client = judge
    body = encode(fake_event("contract-slow-1"))

    async def scenario():
        response = await _post(app, body, {"X-Signature": signed(body)})
        # Processing has begun and is now parked inside the judge...
        await asyncio.wait_for(judge.started.wait(), timeout=5)
        # ...yet the HTTP response already completed with 204 while the
        # judge is still blocked, and no reply has been produced yet.
        assert response.status_code == 204
        assert not judge.release.is_set()
        assert "测试回复" not in [call["message"] for call in calls]
        judge.release.set()
        return response

    response = asyncio.run(test_app.request_with_worker(app, scenario))
    assert response.status_code == 204
    # After release + drain (inside request_with_worker) processing completes.
    assert [call["message"] for call in calls].count("测试回复") == 1


# ---------------------------------------------------------------------------
# Registered known differences (v4 contract vs current behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_kd_c01_message_without_message_id_is_ignored(setup_app):
    app, calls, _, _ = setup_app
    event = fake_event("unused")
    del event["message_id"]
    step = signed_step(event)
    statuses = run_posts(app, [step, step])
    assert statuses == [204, 204]
    assert calls == []  # v4 expectation: ignored twice, never processed


@pytest.mark.timeout(10)
def test_kd_c02_message_without_self_id_is_ignored(setup_app):
    app, calls, _, _ = setup_app
    statuses = run_posts(app, [signed_step(fake_event("contract-noself-1", self_id=None))])
    assert statuses == [204]
    assert calls == []  # v4 expectation: ignored, no onboarding, no reply


@pytest.mark.timeout(10)
def test_kd_c03_dedup_key_includes_self_id(setup_app):
    app, _, _, _ = setup_app
    # Isolation: the two events differ ONLY in self_id — same user_id, same
    # message_id, same text — so any difference in handling is attributable
    # to the self_id dimension alone.
    # Observation point: count consumer dispatches, NOT sends. The delivery
    # ledger is keyed by (user_id, turn_id) with turn_id == message_id, so
    # even a correctly re-claimed second event may legitimately have its
    # *send* suppressed by that ledger; counting sends would mask whether
    # the acceptance/dedup layer swallowed the event before dispatch.
    # (Baseline probe: dispatch count is 1 — the second event never reaches
    # the consumer — while sends show onboarding + one reply.)
    processor = app.state.processor
    original_dispatch = processor.dispatch
    dispatched: list[dict] = []

    async def counting_dispatch(event):
        dispatched.append(event)
        return await original_dispatch(event)

    processor.dispatch = counting_dispatch
    first = fake_event("contract-shared-id", user="90001", self_id="900000001")
    second = fake_event("contract-shared-id", user="90001", self_id="900000002")
    statuses = run_posts(app, [signed_step(first), signed_step(second)])
    assert statuses == [204, 204]
    # v4 expectation: distinct self_id => distinct claim => both dispatched.
    assert len(dispatched) == 2


@pytest.mark.timeout(10)
def test_kd_c04_group_event_does_not_claim_message_id(setup_app):
    app, calls, _, _ = setup_app
    group = {"post_type": "message", "message_type": "group", "group_id": 80001,
             "user_id": "90001", "message_id": "contract-poison-1",
             "message": "群里的话", "raw_message": "群里的话"}
    statuses = run_posts(
        app, [signed_step(group), signed_step(fake_event("contract-poison-1"))]
    )
    assert statuses == [204, 204]
    assert [call["message"] for call in calls].count("测试回复") == 1


@pytest.mark.timeout(10)
def test_kd_c04_malformed_dict_does_not_claim_message_id(setup_app):
    app, calls, _, _ = setup_app
    statuses = run_posts(
        app,
        [signed_step({"message_id": "contract-poison-2"}),
         signed_step(fake_event("contract-poison-2"))],
    )
    assert statuses == [204, 204]
    assert [call["message"] for call in calls].count("测试回复") == 1


@pytest.mark.timeout(10)
def test_kd_c05_ignore_log_carries_reason_code_and_type_summary(setup_app):
    """KD-C05 改判（N3）：群消息不再走 Ignore，而是群聊子链观察登记。

    改判依据：NapCat 全接口方案 C1/C3 与 §6 N3——群消息经 classify 进
    群聊子链，只观察登记（event_type="group_message"），不再落
    ignored/not_private。Ignore 的 reason_code + event_type_summary
    契约本身不废止，继续由 malformed 等真 Ignore 路径覆盖（如
    test_kd_c06 与 golden 的 malformed 系列夹具）。函数名保留原名以
    不动登记册引用，断言按 N3 新口径改判如下。

    报文补 self_id：真实 NapCat 群报文恒带 self_id，且无 self_id 的
    消息按 KD-C02 口径本就走 Ignore（no_self_id）——本例验证的是
    「合格群消息」的登记口径，故与真实报文对齐补齐该字段，其余不变。
    """
    app, _, _, settings = setup_app
    group = {"post_type": "message", "message_type": "group", "group_id": 80001,
             "user_id": "90001", "message_id": "contract-log-1", "self_id": "900000001",
             "message": "群里的话", "raw_message": "群里的话"}
    run_posts(app, [signed_step(group)])
    records = [
        json.loads(line)
        for line in (settings.root / "logs/events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert records and records[-1]["event_type"] == "group_message"
    assert "group" in str(records[-1].get("event_type_summary", ""))


@pytest.mark.timeout(10)
def test_kd_c06_non_json_body_uses_ignore_path(setup_app):
    app, calls, _, _ = setup_app
    body = b"{not json"
    statuses = run_posts(app, [(body, {"X-Signature": signed(body)})])
    assert statuses == [204]
    assert calls == []


@pytest.mark.timeout(10)
def test_kd_c06_non_object_json_uses_ignore_path(setup_app):
    app, calls, _, _ = setup_app
    body = b"[1, 2, 3]"
    statuses = run_posts(app, [(body, {"X-Signature": signed(body)})])
    assert statuses == [204]
    assert calls == []


@pytest.mark.timeout(10)
def test_kd_c07_message_without_user_id_is_ignored_at_dispatch(setup_app):
    app, _, _, _ = setup_app
    event = fake_event("contract-nouser-1")
    del event["user_id"]
    task = asyncio.run(app.state.processor.dispatch(event))
    assert task is None  # v4 expectation: Ignore terminates before any processing


def _second_app(settings):
    """A fresh app instance over the same settings/root, simulating a restart."""
    calls = []

    class FakeSender:
        async def send_private_msg(self, user_id, message, *, priority="normal"):
            calls.append({"user_id": int(user_id), "message": message})
            return {"status": "ok"}

        async def send_alert_private_msg(self, user_id, message):
            return await self.send_private_msg(user_id, message)

    class FakeAlerter:
        async def alert(self, *_args, **_kwargs):
            return None

    class FakeLLM:
        async def generate(self, _prompt):
            return "测试回复"

        async def generate_json(self, _prompt):
            return json.dumps({"bubbles": ["测试回复"]}, ensure_ascii=False)

    class SafeJudgeLLM:
        async def generate(self, _prompt):
            return "NO"

    async def no_sleep(_delay):
        return None

    app = create_app(
        settings,
        sender=FakeSender(),
        alerter=FakeAlerter(),
        sleep=no_sleep,
        random_uniform=lambda _a, _b: 0,
        brain=CompanionBrain(settings.persona, FakeLLM()),
    )
    app.state.processor.safety.judge = ModelLLMJudge(SafeJudgeLLM())
    return app, calls


@pytest.mark.timeout(10)
def test_kd_c08_dedup_survives_restart(setup_app):
    app, calls, _, settings = setup_app
    step = signed_step(fake_event("contract-restart-1"))
    assert run_posts(app, [step]) == [204]
    assert [call["message"] for call in calls].count("测试回复") == 1
    log_path = settings.root / "logs/events.jsonl"
    lines_before = len(log_path.read_text().splitlines())
    restarted, calls_after = _second_app(settings)
    assert run_posts(restarted, [step]) == [204]
    # v4 expectation: the persisted claim swallows the duplicate at the
    # acceptance boundary — no reprocessing, hence no new event-log record.
    assert len(log_path.read_text().splitlines()) == lines_before
    assert calls_after == []


@pytest.mark.timeout(10)
def test_kd_c08_crisis_duplicate_is_not_resent_after_restart(setup_app):
    app, calls, _, settings = setup_app
    step = signed_step(fake_event("contract-restart-2", text="想自杀"))
    assert run_posts(app, [step]) == [204]
    assert len(calls) == 2  # onboarding + fixed crisis reply
    restarted, calls_after = _second_app(settings)
    assert run_posts(restarted, [step]) == [204]
    assert calls_after == []  # v4 expectation: duplicate crisis is claimed, not resent


@pytest.mark.timeout(10)
def test_kd_c09_claim_capacity_is_partitioned(setup_app):
    app, _, _, _ = setup_app
    store = getattr(app.state, "claim_store", None)
    partitions = set(getattr(store, "partitions", ()) or ())
    assert {"ordinary", "reserved"} <= partitions


@pytest.mark.timeout(10)
def test_kd_c10_inbound_queue_is_bounded(setup_app):
    app, _, _, _ = setup_app
    # Behavioural probe (not a maxsize attribute check), with the capacity
    # under the test's own control: the 100-post version could never see a
    # 503 if a future bounded queue happened to have capacity exactly 100
    # (false registration), and a blocking publish would eat the whole
    # case timeout. So inject a small bounded queue before posting:
    # ``create_app`` stores the bus as ``app.state.event_bus`` and the
    # webhook's publish path is ``EventBus.publish`` ->
    # ``await self.queue.put(event)``, i.e. it goes through exactly this
    # ``queue`` attribute (consumer deliberately not started, so nothing
    # drains it; the app is built fresh for this case, so the swap cannot
    # pollute other cases).
    capacity = 3
    app.state.event_bus.queue = asyncio.Queue(maxsize=capacity)
    # Distinct message_ids are required — duplicates short-circuit to 204
    # at the dedup check and never reach the queue.

    async def scenario():
        statuses = []
        for index in range(capacity + 1):
            body = encode(fake_event(f"contract-overload-{index}"))
            try:
                response = await asyncio.wait_for(
                    _post(app, body, {"X-Signature": signed(body)}), timeout=2
                )
            except TimeoutError:
                raise AssertionError(
                    f"post {index + 1} blocked in publish instead of "
                    "returning 503 once the injected queue was full"
                ) from None
            statuses.append(response.status_code)
        return statuses

    statuses = asyncio.run(scenario())  # note: consumer deliberately not started
    assert statuses[0] == 204  # the boundary accepts before it saturates
    assert 503 in statuses  # v4 expectation: overload is refused, not queued forever


@pytest.mark.timeout(10)
def test_kd_c11_crisis_lane_mechanism_exists(setup_app):
    app, _, _, _ = setup_app
    assert getattr(app.state, "critical_executor", None) is not None
