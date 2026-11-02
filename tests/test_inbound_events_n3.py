"""N3 inbound-event behaviour tests (NapCat 全接口方案 §6 N3 / C1/C3/C6/C7).

Fixed log contract under test (event_type values are pinned):

- group message observation/registration -> ``group_message``
  (crisis keyword hit: ``reason_code="crisis_keyword"``; anonymous /
  system-style: ``reason_code="registered_only"``; shed on a full group
  queue: ``reason_code="shed"``);
- notice registration -> ``notice``; ops escalation (bot kicked
  ``kick_me`` / bot muted ``group_ban`` with user_id == self_id /
  ``bot_offline``) appends ``ops_alert`` *after* the notice record
  (reason_code ``bot_kicked`` / ``bot_muted`` / ``bot_offline``);
- request disposition -> ``request``; meta -> ``meta_event``;
  self echo -> ``message_sent``;
- private-chat behaviour and logging are unchanged.

Everything runs through the webhook plus in-memory / on-disk assertions,
reusing ``tests.test_app.setup_app``/``run`` helpers and
``tests.test_adapter_contract_p0``'s ``_post``/``signed_step``/``fake_event``.
The direct ``handle_request`` cases use its landed signature
``handle_request(proc, ev, *, store, channel, policy)`` with a classified
``RequestEvent`` (via ``FakeAdapter.classify``, the same normalization the
webhook path uses), a minimal proc stub (metrics + _log only, exactly the
surface the events module is allowed to touch), and a ``try_begin`` first
— mirroring the acceptance boundary, which registers the disposition
before the event consumer drives it.
"""

import asyncio
import json
import sqlite3

import pytest

from companion.pipeline import STAGE_ORDER
from companion.safety import crisis_reply
from tests import test_app
from tests.test_adapter_contract_p0 import fake_event, run_posts, signed_step

setup_app = test_app.setup_app
SELF_ID = "900000001"

EXPECTED_PRIVATE_STAGE_ORDER = (
    "TransportGate",
    "Normalize",
    "Admission",
    "SafetyJudge",
    "InboundRateLimit",
    "Command",
    "MemoryLoad",
    "Persona",
    "Respond",
    "OutputGuard",
    "Deliver",
)


# ---------------------------------------------------------------------------
# Event builders / log readers
# ---------------------------------------------------------------------------


def group_event(mid, user="90001", text="群里的话", **extra):
    event = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": mid,
        "group_id": 80001,
        "user_id": user,
        "message": text,
        "raw_message": text,
        "sender": {"user_id": int(user), "nickname": "fake-user", "card": ""},
        "font": 0,
    }
    event.update(extra)
    return event


def notice_event(ntype, **extra):
    event = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "notice",
        "notice_type": ntype,
    }
    event.update(extra)
    return event


def request_event(flag, **extra):
    event = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "request",
        "request_type": "friend",
        "user_id": "90001",
        "comment": "fake request",
        "flag": flag,
    }
    event.update(extra)
    return event


def read_records(settings):
    path = settings.root / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def records_of(settings, event_type):
    return [r for r in read_records(settings) if r.get("event_type") == event_type]


def request_rows(settings):
    db_path = settings.root / "memory.db"
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT status, decision FROM request_dispositions ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Subchain contract: GROUP_/EVENT_ tuples, scene_for / subchain_for mapping,
# private STAGE_ORDER byte-identical to the baseline 11-tuple
# ---------------------------------------------------------------------------


def _scene_and_chain(events_mod, raw):
    from companion.adapters.fake import FakeAdapter

    classified = FakeAdapter().classify(raw)
    scene = events_mod.scene_for(classified)
    chain = events_mod.subchain_for(classified)
    return scene, chain


@pytest.mark.timeout(10)
def test_subchain_contract_and_scene_mapping():
    from companion.ingress import events as events_mod

    assert isinstance(events_mod.GROUP_SUBCHAIN, tuple) and events_mod.GROUP_SUBCHAIN
    assert isinstance(events_mod.EVENT_SUBCHAIN, tuple) and events_mod.EVENT_SUBCHAIN
    assert all(isinstance(s, str) and s for s in events_mod.GROUP_SUBCHAIN)
    assert all(isinstance(s, str) and s for s in events_mod.EVENT_SUBCHAIN)
    assert events_mod.GROUP_SUBCHAIN != events_mod.EVENT_SUBCHAIN
    # Group subchain default form (plan C3): parse/classify/observe only —
    # no generation stage may be present.
    assert "Respond" not in events_mod.GROUP_SUBCHAIN
    assert any("Observ" in s or "Audit" in s for s in events_mod.GROUP_SUBCHAIN)
    # Event subchain carries the request-handling stage (default off).
    assert any("Request" in s for s in events_mod.EVENT_SUBCHAIN)

    # Private chain is byte-identical to the baseline 11 stages.
    assert STAGE_ORDER == EXPECTED_PRIVATE_STAGE_ORDER
    assert len(STAGE_ORDER) == 11

    scene, chain = _scene_and_chain(events_mod, fake_event("n3-scene-priv"))
    assert scene == "private_chat"
    # Private messages stay on the existing private chain: subchain_for
    # returns the empty tuple for them (no event/group subchain).
    assert chain == ()

    scene, chain = _scene_and_chain(events_mod, group_event("n3-scene-group"))
    assert scene == "group_chat"
    assert chain == events_mod.GROUP_SUBCHAIN

    scene, chain = _scene_and_chain(
        events_mod, notice_event("friend_add", user_id="90001")
    )
    assert scene == "event_notice"
    assert chain == events_mod.EVENT_SUBCHAIN

    scene, chain = _scene_and_chain(events_mod, request_event("n3-scene-flag"))
    assert scene == "event_request"
    assert chain == events_mod.EVENT_SUBCHAIN

    scene, _chain = _scene_and_chain(
        events_mod,
        {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "self_id": SELF_ID,
            "status": {"online": True, "good": True},
            "interval": 5000,
        },
    )
    assert scene == "system"


# ---------------------------------------------------------------------------
# Group observation: no reply, no private memory, no alerts
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_group_message_is_observed_only(setup_app):
    app, calls, alerter, settings = setup_app
    statuses = run_posts(app, [signed_step(group_event("n3-group-1"))])
    assert statuses == [204]
    assert calls == []  # zero sends
    observed = records_of(settings, "group_message")
    assert len(observed) == 1
    # No private conversation history was created for the group speaker.
    history = asyncio.run(app.state.processor.conversation.get_history("90001"))
    assert history == []
    # No alert side effects (injected alerter or alerts.jsonl).
    assert alerter.items == []
    alerts_path = settings.root / "logs" / "alerts.jsonl"
    assert not alerts_path.exists() or alerts_path.read_text(encoding="utf-8").strip() == ""


@pytest.mark.timeout(10)
def test_group_crisis_keyword_never_enters_private_crisis_chain(setup_app):
    app, calls, alerter, settings = setup_app
    statuses = run_posts(app, [signed_step(group_event("n3-group-crisis", text="想自杀"))])
    assert statuses == [204]
    assert calls == []  # no crisis copy is sent to anyone for group speech
    assert alerter.items == []  # and no human alert fires
    observed = records_of(settings, "group_message")
    assert len(observed) == 1
    assert observed[-1].get("reason_code") == "crisis_keyword"

    # Control case, same app: the same words in private chat DO take the
    # private crisis path (fixed crisis copy sent to the user).
    statuses = run_posts(app, [signed_step(fake_event("n3-priv-crisis", text="想自杀"))])
    assert statuses == [204]
    expected = crisis_reply(settings.safety["helplines"], False, settings.safety)
    assert any(call["message"] == expected for call in calls)


@pytest.mark.timeout(10)
def test_group_claim_isolation_and_dedup(setup_app):
    app, calls, _, settings = setup_app
    step = signed_step(group_event("n3-iso-1"))
    statuses = run_posts(app, [step, step])
    assert statuses == [204, 204]
    # The same group message_id is observed exactly once.
    assert len(records_of(settings, "group_message")) == 1
    # A private message reusing the id is unaffected (golden口径一致):
    # group observation must not poison the private claim space.
    statuses = run_posts(app, [signed_step(fake_event("n3-iso-1"))])
    assert statuses == [204]
    assert [call["message"] for call in calls].count("测试回复") == 1


@pytest.mark.timeout(10)
def test_message_sent_registers_without_claiming(setup_app):
    app, calls, _, settings = setup_app
    sent = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "message_sent",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": "n3-ms-1",
        "user_id": SELF_ID,
        "target_id": 90001,
        "message": "机器人自己发出的私聊",
        "raw_message": "机器人自己发出的私聊",
    }
    statuses = run_posts(app, [signed_step(sent)])
    assert statuses == [204]
    assert calls == []
    assert len(records_of(settings, "message_sent")) == 1
    # message_sent never enters the conversation claim key space: a private
    # message with the same id is still processed normally.
    statuses = run_posts(app, [signed_step(fake_event("n3-ms-1"))])
    assert statuses == [204]
    assert [call["message"] for call in calls].count("测试回复") == 1


# ---------------------------------------------------------------------------
# Requests through the webhook: disposed exactly once, persisted
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_request_webhook_disposes_exactly_once(setup_app):
    app, calls, _, settings = setup_app
    step = signed_step(request_event("n3-webhook-flag-1"))
    statuses = run_posts(app, [step, step])
    assert statuses == [204, 204]
    assert calls == []
    rows = request_rows(settings)
    assert len(rows) == 1
    assert rows[0] == ("done", "manual")  # default policy: manual disposition
    request_records = records_of(settings, "request")
    assert len(request_records) == 1
    # The second delivery is recorded as a duplicate (接入层口径:
    # reason_code="duplicate_delivery"; see report note).
    all_records = read_records(settings)
    assert any(
        r.get("reason_code") == "duplicate_delivery" or "duplicate" in str(r.get("event_type"))
        for r in all_records
    ), all_records


# ---------------------------------------------------------------------------
# Requests end-to-end through the Fake channel (direct handle_request)
# ---------------------------------------------------------------------------


class ApprovePolicy:
    """Policy stub: ``decide(ev) -> "approve"`` (the landed policy shape)."""

    def decide(self, _ev):
        return "approve"


class _AmbiguousResult(dict):
    """Channel result readable both as a dict and via .outcome/.status."""

    def __init__(self):
        super().__init__({"status": "ambiguous", "outcome": "ambiguous"})
        self.status = "ambiguous"
        self.outcome = "ambiguous"


class AmbiguousChannel:
    """Channel替身: ``dispose`` reports an ambiguous outcome, counted."""

    def __init__(self):
        self.calls = 0

    async def dispose(self, _ev, *, approve):
        self.calls += 1
        return _AmbiguousResult()


class ProcStub:
    """Minimal processor handle: exactly metrics + _log (events-module面)."""

    def __init__(self):
        from companion.pipeline.observers import StageMetrics

        self.metrics = StageMetrics()
        self.records = []

    def _log(self, event_type, length, allowed, latency_ms, *,
             reason_code=None, event_type_summary=None, extra=None):
        record = {
            "event_type": event_type,
            "message_length": length,
            "whitelist_hit": allowed,
            "processing_latency_ms": latency_ms,
        }
        if reason_code is not None:
            record["reason_code"] = reason_code
        if event_type_summary is not None:
            record["event_type_summary"] = event_type_summary
        if extra:
            record.update(extra)
        self.records.append(record)


def _make_store(tmp_path):
    from companion.memory import Database
    from companion.memory.requests import SqliteRequestStore

    return SqliteRequestStore(Database(tmp_path / "requests-e2e.db"))


def _gateway_channel(fake):
    from companion.adapters.gateway import PRINCIPAL_SYSTEM, AuthorizationContext
    from companion.ingress.events import GatewayRequestChannel

    # N4 改判（接线形态，非断言口径）：处置通道自 N4 起须显式注入
    # AuthorizationContext（处置动作参数只有 flag、目标派生为
    # account，故 account=True），且 L2 canonical 路径默认 dry_run
    # 为真，策略须在 policies 中显式关闭（见 _n4_policies）。
    return GatewayRequestChannel(
        fake.action_gateway,
        principal=PRINCIPAL_SYSTEM,
        confirmed=True,
        authorization=AuthorizationContext(authorizer="n3-test", account=True),
    )


def _n4_policies():
    """N4 L2 canonical 口径：逐动作 enabled + 显式 dry_run=false。"""
    return {
        "set_friend_add_request": {"enabled": True, "dry_run": False},
        "set_group_add_request": {"enabled": True, "dry_run": False},
    }


async def _handle(events_mod, proc, store, channel, policy, raw):
    """Mirror the webhook path: classify -> try_begin -> handle_request."""
    from companion.adapters.fake import FakeAdapter
    from companion.domain.requests import request_key

    ev = FakeAdapter().classify(raw)
    key = request_key(ev.self_id, ev.request_type, ev.sub_type, ev.flag)
    store.try_begin(key, user_id=ev.user_id, group_id=ev.group_id)
    await events_mod.handle_request(proc, ev, store=store, channel=channel, policy=policy)


def _record(store, flag, request_type="friend", sub_type=""):
    from companion.domain.requests import request_key

    return store.get(request_key(SELF_ID, request_type, sub_type, flag))


@pytest.mark.timeout(10)
def test_handle_request_fake_channel_end_to_end(tmp_path):
    from companion.adapters.fake import FakeAdapter
    from companion.ingress import events as events_mod

    fake = FakeAdapter(policies=_n4_policies())
    channel = _gateway_channel(fake)
    # F4: GatewayRequestChannel 不得自造 principal——白名单外的主体
    # （如 llm_tool）构造即 ValueError，授权上下文必须由接线层注入。
    # N4 追加：authorization 缺失同样构造即 ValueError。
    from companion.adapters.gateway import AuthorizationContext
    from companion.ingress.events import GatewayRequestChannel

    with pytest.raises(ValueError):
        GatewayRequestChannel(
            fake.action_gateway,
            principal="llm_tool",
            confirmed=True,
            authorization=AuthorizationContext(authorizer="n3-test", account=True),
        )
    with pytest.raises(ValueError):
        GatewayRequestChannel(
            fake.action_gateway,
            principal="system",
            confirmed=True,
            authorization=None,
        )
    store = _make_store(tmp_path)
    proc = ProcStub()
    raw = request_event("n3-flag-1")

    asyncio.run(_handle(events_mod, proc, store, channel, ApprovePolicy(), raw))
    record = _record(store, "n3-flag-1")
    assert record is not None
    assert record.status == "done"
    assert record.decision == "approved"
    action_calls = [c for c in fake.transport.calls if c["action"] == "set_friend_add_request"]
    assert len(action_calls) == 1

    # Re-handling the same request is blocked by claim_processing / the
    # terminal state: no second platform call, state untouched.
    asyncio.run(_handle(events_mod, proc, store, channel, ApprovePolicy(), raw))
    action_calls = [c for c in fake.transport.calls if c["action"] == "set_friend_add_request"]
    assert len(action_calls) == 1
    assert _record(store, "n3-flag-1").status == "done"


@pytest.mark.timeout(10)
def test_handle_request_ambiguous_channel_is_terminal(tmp_path):
    from companion.ingress import events as events_mod

    channel = AmbiguousChannel()
    store = _make_store(tmp_path)
    proc = ProcStub()
    raw = request_event("n3-flag-amb")

    asyncio.run(_handle(events_mod, proc, store, channel, ApprovePolicy(), raw))
    record = _record(store, "n3-flag-amb")
    assert record is not None
    assert record.status == "ambiguous"
    calls_after_first = channel.calls
    assert calls_after_first >= 1

    # ambiguous is terminal (不释放、不重做): a second drive attempt must
    # not call the channel again.
    asyncio.run(_handle(events_mod, proc, store, channel, ApprovePolicy(), raw))
    assert channel.calls == calls_after_first
    assert _record(store, "n3-flag-amb").status == "ambiguous"


# ---------------------------------------------------------------------------
# Notice coalescing: same key merges into a single registration
# ---------------------------------------------------------------------------


def _coalesced_observable(app, settings) -> bool:
    metrics = app.state.processor.metrics
    try:
        if "coalesced" in metrics.render_text():
            return True
    except Exception:  # noqa: BLE001
        pass
    counts = getattr(metrics, "event_counts", {}) or {}
    return any("coalesced" in str(key) for key in counts)


@pytest.mark.timeout(10)
def test_notice_group_card_coalesces(setup_app):
    app, calls, _, settings = setup_app
    card = notice_event(
        "group_card", group_id=80001, user_id="90001",
        card_new="fake-card-new", card_old="fake-card-old",
    )
    body, headers = signed_step(card)

    async def scenario():
        # Coalescing is an in-flight merge (C7 入队前合并): the three
        # deliveries must arrive while the first is still queued, so the
        # consumer is only started after all three posts. Everything runs
        # in one event loop — the bus queues must not cross loops.
        from tests.test_adapter_contract_p0 import _post

        statuses = []
        for _ in range(3):
            response = await _post(app, body, headers)
            statuses.append(response.status_code)
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            await test_app.drain(app)
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        return statuses

    statuses = asyncio.run(scenario())
    assert statuses == [204, 204, 204]
    assert calls == []
    assert len(records_of(settings, "notice")) == 1  # merged registration
    assert _coalesced_observable(app, settings)


# ---------------------------------------------------------------------------
# Meta heartbeat registration
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_meta_heartbeat_registers_with_status_fields(setup_app):
    app, calls, _, settings = setup_app
    heartbeat = {
        "time": 1767225600,
        "self_id": SELF_ID,
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "status": {"online": True, "good": True},
        "interval": 5000,
    }
    statuses = run_posts(app, [signed_step(heartbeat)])
    assert statuses == [204]
    assert calls == []
    records = records_of(settings, "meta_event")
    assert len(records) == 1
    record = records[-1]
    online = record.get("heartbeat_online", record.get("status_online", record.get("online")))
    good = record.get("heartbeat_good", record.get("status_good", record.get("good")))
    assert online is True
    assert good is True


# ---------------------------------------------------------------------------
# Saturation: event storms never starve the private / crisis lanes
# (app built the test_pipeline_saturation_p3 way, small N3 lane capacities
# injected through settings.app["pipeline"])
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_notice_flood_does_not_starve_requests(tmp_path, monkeypatch):
    from tests.test_adapter_contract_p0 import _post
    from tests.test_pipeline_saturation_p3 import build_app, wait_until

    app, sender, settings = build_app(
        tmp_path,
        monkeypatch,
        pipeline={
            "event_queue": 1,
            "group_queue": 2,
            "request_queue": 4,
            "ordinary_queue": 10,
            "control_queue": 10,
            "critical_queue": 10,
        },
    )
    notices = [
        notice_event(
            "group_card", group_id=80001, user_id=f"900{i}",
            card_new="fake-new", card_old="fake-old",
        )
        for i in range(3)
    ]

    async def scenario():
        # One loop throughout (bus queues must not cross event loops).
        # Fill first with no consumer: the event lane (capacity 1) fills
        # with distinct-key card notices; shed semantics keep answering
        # 204 for registration-class events.
        fill_statuses = []
        for event in notices:
            body, headers = signed_step(event)
            response = await _post(app, body, headers)
            fill_statuses.append(response.status_code)
        # With the event lane saturated, a request still gets its reserved
        # capacity: accepted (204) and driven to a persisted disposition.
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            body, headers = signed_step(request_event("n3-sat-flag"))
            response = await _post(app, body, headers)
            done = await wait_until(
                lambda: request_rows(settings) == [("done", "manual")]
            )
            return fill_statuses, response.status_code, done
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    fill_statuses, status, done = asyncio.run(scenario())
    assert fill_statuses == [204, 204, 204]
    assert status == 204
    assert done, "request was not disposed while the notice lane was saturated"
    assert request_rows(settings) == [("done", "manual")]
    assert sender.calls == []


@pytest.mark.timeout(10)
def test_group_flood_does_not_starve_private_or_crisis(tmp_path, monkeypatch):
    from tests.test_adapter_contract_p0 import _post
    from tests.test_pipeline_saturation_p3 import (
        build_app,
        expected_crisis,
        mark_onboarded,
        messages,
        wait_until,
    )

    app, sender, settings = build_app(
        tmp_path,
        monkeypatch,
        pipeline={
            "event_queue": 4,
            "group_queue": 1,
            "request_queue": 4,
            "ordinary_queue": 10,
            "control_queue": 10,
            "critical_queue": 10,
        },
    )
    # Ordinary and crisis traffic use different users: a crisis message
    # legitimately cancels the same user's in-flight ordinary stream
    # (baseline preemption semantics), which would mask starvation here.
    mark_onboarded(app, "10001", "10002")

    async def scenario():
        # One loop throughout (bus queues must not cross event loops).
        # Fill the group lane (capacity 1) with no consumer running;
        # excess group traffic sheds with 204, never 503.
        fill_statuses = []
        for index in range(3):
            body, headers = signed_step(group_event(f"n3-flood-{index}"))
            response = await _post(app, body, headers)
            fill_statuses.append(response.status_code)
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            statuses = []
            for event in (
                fake_event("n3-flood-priv", user="10001", text="普通消息"),
                fake_event("n3-flood-crisis", user="10002", text="想自杀"),
            ):
                body, headers = signed_step(event)
                response = await _post(app, body, headers)
                statuses.append(response.status_code)
            ok = await wait_until(
                lambda: "测试回复" in messages(sender)
                and expected_crisis(settings) in messages(sender)
            )
            return fill_statuses, statuses, ok
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    fill_statuses, statuses, ok = asyncio.run(scenario())
    assert fill_statuses == [204, 204, 204]
    assert statuses == [204, 204]
    assert ok, "private / crisis replies were starved by the group flood"


# ---------------------------------------------------------------------------
# Codex Review fixes (F1/F2/F3/F6): group claim partition isolation,
# pending-request startup sweep, event-lane exception isolation,
# notice rate budget keyed by (self_id, group, family)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_group_claims_use_isolated_group_partition(tmp_path):
    # F1 (storage level): group claims fill only the group partition —
    # they must not consume the ordinary partition that private claims
    # depend on, and counts()/partitions keep their pinned two-key shape.
    from companion.memory import Database
    from companion.memory.claims import LANE_GROUP, ClaimOutcome, SqliteClaimStore

    store = SqliteClaimStore(
        Database(tmp_path / "claims-f1.db"),
        ordinary_capacity=1,
        group_capacity=2,
    )
    key = lambda mid: ("onebot", SELF_ID, mid)  # noqa: E731
    assert store.try_claim(key("g-1"), LANE_GROUP) is ClaimOutcome.CLAIMED
    assert store.try_claim(key("g-2"), LANE_GROUP) is ClaimOutcome.CLAIMED
    assert store.try_claim(key("g-3"), LANE_GROUP) is ClaimOutcome.FULL
    # The ordinary lane is untouched by the saturated group partition.
    assert store.try_claim(key("p-1"), "ordinary") is ClaimOutcome.CLAIMED
    assert store.counts() == {"ordinary": 1, "reserved": 0}
    assert set(store.partitions) == {"ordinary", "reserved"}


@pytest.mark.timeout(10)
def test_group_claims_do_not_starve_private_claims_webhook(tmp_path, monkeypatch):
    # F1 (webhook level): with ordinary claim capacity 2, two drained
    # group messages must not leave the private claim partition full —
    # the private message still gets 204 and a reply, never 503.
    from tests.test_adapter_contract_p0 import _post
    from tests.test_pipeline_saturation_p3 import (
        build_app,
        mark_onboarded,
        messages,
        wait_until,
    )

    app, sender, settings = build_app(
        tmp_path,
        monkeypatch,
        pipeline={
            "ordinary_claim_capacity": 2,
            "group_claim_capacity": 10,
            "ordinary_queue": 10,
            "control_queue": 10,
            "critical_queue": 10,
            "group_queue": 10,
            "event_queue": 10,
            "request_queue": 10,
        },
    )
    mark_onboarded(app, "10001")

    async def scenario():
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            group_statuses = []
            for index in range(2):
                body, headers = signed_step(group_event(f"n3-f1-group-{index}"))
                response = await _post(app, body, headers)
                group_statuses.append(response.status_code)
            drained = await wait_until(
                lambda: len(records_of(settings, "group_message")) == 2
            )
            body, headers = signed_step(
                fake_event("n3-f1-priv", user="10001", text="普通消息")
            )
            response = await _post(app, body, headers)
            replied = await wait_until(lambda: "测试回复" in messages(sender))
            return group_statuses, drained, response.status_code, replied
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    group_statuses, drained, status, replied = asyncio.run(scenario())
    assert group_statuses == [204, 204]
    assert drained, "group messages were not drained"
    assert status == 204
    assert replied, "private reply was starved by group claims (503/full)"


@pytest.mark.timeout(10)
def test_pending_request_is_swept_when_consumer_starts(tmp_path, monkeypatch):
    # F2: a request persisted as pending by a previous process (posted
    # with no consumer running) is re-driven by the startup sweep of a
    # new app over the same database, landing done/manual.
    from tests.test_adapter_contract_p0 import _post
    from tests.test_pipeline_saturation_p3 import build_app, wait_until

    app1, _sender1, settings1 = build_app(tmp_path, monkeypatch)

    async def post_only():
        body, headers = signed_step(request_event("n3-f2-flag"))
        response = await _post(app1, body, headers)
        return response.status_code

    assert asyncio.run(post_only()) == 204
    assert request_rows(settings1) == [("pending", None)]

    app2, _sender2, settings2 = build_app(tmp_path, monkeypatch)

    async def scenario():
        consumer = asyncio.create_task(app2.state.event_consumer())
        try:
            return await wait_until(
                lambda: request_rows(settings2) == [("done", "manual")]
            )
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    assert asyncio.run(scenario()), "pending request was not swept on startup"
    assert request_rows(settings2) == [("done", "manual")]


@pytest.mark.timeout(10)
def test_event_lane_survives_a_bad_notice_envelope(tmp_path, monkeypatch):
    # F3: one poisonous envelope (register_notice raises on it) must not
    # kill the event lane loop — a later request is still disposed.
    from tests.test_adapter_contract_p0 import _post
    from tests.test_pipeline_saturation_p3 import build_app, wait_until

    app, sender, settings = build_app(
        tmp_path,
        monkeypatch,
        pipeline={
            "ordinary_queue": 10,
            "control_queue": 10,
            "critical_queue": 10,
            "group_queue": 10,
            "event_queue": 10,
            "request_queue": 10,
        },
    )

    async def scenario():
        consumer = asyncio.create_task(app.state.event_consumer())
        try:
            assert app.state.event_bus.try_publish(
                "notice", {"classified": object()}
            )
            # Give the loop time to hit (and, after the fix, survive)
            # the bad envelope before the request arrives.
            await asyncio.sleep(0.3)
            body, headers = signed_step(request_event("n3-f3-flag"))
            response = await _post(app, body, headers)
            done = await wait_until(
                lambda: request_rows(settings) == [("done", "manual")]
            )
            return response.status_code, done
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    status, done = asyncio.run(scenario())
    assert status == 204
    assert done, "event lane loop died on a bad notice envelope"
    assert sender.calls == []


@pytest.mark.timeout(10)
def test_notice_rate_budget_is_keyed_by_group_and_family():
    # F6 (方案 C7): the budget key is (self_id, group, family) — the
    # same notice type in another group is unaffected, notify sub-types
    # are separate families, and a second hit on the same key is shed.
    from companion.adapters.base import NoticeEvent
    from companion.ingress.events import NoticeRateBudget

    def ev(group_id, notice_type="group_card", sub_type=None):
        return NoticeEvent(
            platform="onebot",
            self_id=SELF_ID,
            notice_type=notice_type,
            sub_type=sub_type,
            user_id="90001",
            group_id=group_id,
            actor_id=None,
            target_id=None,
            message_id=None,
            raw={},
        )

    budget = NoticeRateBudget(limit=1)
    assert budget.allow(ev("80001"))
    assert budget.allow(ev("80002"))  # different group: unaffected
    assert not budget.allow(ev("80001"))  # same (group, family): rejected
    # notify families split by sub_type within the same group.
    assert budget.allow(ev("80001", "notify", "input_status"))
    assert budget.allow(ev("80001", "notify", "poke"))
    assert not budget.allow(ev("80001", "notify", "input_status"))
