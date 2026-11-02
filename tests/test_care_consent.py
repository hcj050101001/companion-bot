"""Synthetic care tests; all OneBot requests use an in-process fake."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from companion.config import ROOT
from companion.memory.care import CarePreferences
from companion.safety import LLMJudge, SafetyGateway
from companion.safety.care import CareSendGate
from companion.tools import HttpOneBotClient
from tests.fake_onebot import make_fake_onebot

USER = "10101"
START = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)  # 09:00 Shanghai


@pytest.fixture
def care(tmp_path, monkeypatch):
    store = CarePreferences(tmp_path / "private" / "care.db")
    fake = make_fake_onebot("test-only")
    original = httpx.AsyncClient

    class FakeHTTP(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.ASGITransport(app=fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeHTTP)
    sender = HttpOneBotClient(
        "http://fake-onebot",
        policies={
            "send_private_msg": {"enabled": True, "rate_limit": 6},
        },
    )
    state = {
        "now": START,
        "allowed": {USER},
        "safe": True,
        "revision": 0,
        "gateway": SafetyGateway([]),
        "config": {"proactive_care": {"enabled": True}},
    }
    gate = CareSendGate(
        store,
        sender,
        whitelist=lambda uid: uid in state["allowed"],
        current_safety=lambda uid: (state["gateway"], state["safe"], state["revision"]),
        config=state["config"],
        clock=lambda: state["now"],
    )
    return store, fake, sender, state, gate


def enable(store, now=START, days=3):
    store.opt_in(USER, now=now, expires=now + timedelta(days=days))


def attempt(gate, work="work-1", created=START):
    return asyncio.run(gate.send(USER, work, created))


def test_opt_in_stop_pause_forget_and_retention(care):
    store, fake, _, state, gate = care
    assert not attempt(gate)
    enable(store)
    store.pause(USER, now=START, until=START + timedelta(hours=1))
    assert not attempt(gate)
    state["now"] += timedelta(hours=1)
    assert attempt(gate)
    store.stop(USER)
    state["now"] += timedelta(days=1)
    assert not attempt(gate, "work-2", state["now"])
    enable(store, state["now"])
    store.forget(USER)
    assert not attempt(gate, "work-2", state["now"])
    assert len(fake.state.calls) == 1
    # Forget erases choices, but short-lived receipts protect against cap bypass.
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM choices").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    store.prune(START + timedelta(days=4))
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "hour,minute,allowed", [(22, 59, True), (23, 0, False), (7, 59, False), (8, 0, True)]
)
def test_quiet_hours_shanghai(care, hour, minute, allowed):
    store, fake, _, state, gate = care
    local = datetime(2026, 1, 2, hour, minute, tzinfo=timezone(timedelta(hours=8)))
    state["now"] = local.astimezone(timezone.utc)
    enable(store, state["now"])
    assert attempt(gate, created=state["now"]) is allowed
    assert len(fake.state.calls) == int(allowed)


def test_expiry_and_flag_fail_closed(care):
    store, fake, _, state, gate = care
    enable(store)
    state["config"]["proactive_care"]["enabled"] = False
    assert not attempt(gate)
    state["config"]["proactive_care"]["enabled"] = True
    state["now"] = START + timedelta(days=3)
    assert not attempt(gate, created=state["now"])
    with pytest.raises(ValueError):
        store.opt_in(USER, now=START, expires=START + timedelta(days=31))
    with pytest.raises(ValueError):
        store.opt_in(USER, now=START.replace(tzinfo=None), expires=START)
    assert not fake.state.calls


def test_whitelist_safety_and_old_work_fail_closed(care):
    store, fake, _, state, gate = care
    enable(store)
    state["allowed"].clear()
    assert not attempt(gate)
    state["allowed"].add(USER)
    state["safe"] = False  # crisis processing takes precedence
    assert not attempt(gate)
    state["safe"] = True
    assert not attempt(gate, created=START - timedelta(hours=24))
    assert not fake.state.calls


def test_duplicate_and_daily_cap_across_reconsent(care):
    store, fake, _, state, gate = care
    enable(store)
    assert attempt(gate)
    store.forget(USER)
    enable(store)
    assert not attempt(gate)
    assert not attempt(gate, "different-work")
    state["now"] += timedelta(days=1)
    assert not attempt(gate, created=state["now"])  # same work ID
    assert attempt(gate, "next-work", state["now"])
    assert len(fake.state.calls) == 2


def test_onebot_rate_cap_and_failed_attempt_not_retried(care):
    store, fake, sender, state, gate = care
    enable(store)
    sender.gateway.policies["send_private_msg"]["rate_limit"] = 1
    assert asyncio.run(sender.send_private_msg(USER, "synthetic"))["status"] == "ok"
    assert not attempt(gate)
    sender.gateway.limiter.calls.clear()
    assert not attempt(gate)  # ambiguous/blocked attempts are not replayed
    assert len(fake.state.calls) == 1


@pytest.mark.parametrize("change", ["withdraw", "whitelist", "crisis", "revision", "flag"])
def test_rechecks_after_async_safety(care, change):
    store, fake, _, state, gate = care
    enable(store)

    class ChangingJudge(LLMJudge):
        async def judge(self, text):
            if change == "withdraw":
                store.stop(USER)
            elif change == "whitelist":
                state["allowed"].clear()
            elif change == "crisis":
                state["safe"] = False
            elif change == "revision":
                state["revision"] += 1
            else:
                state["config"]["proactive_care"]["enabled"] = False
            return False

    state["gateway"] = SafetyGateway([], ChangingJudge())
    assert not attempt(gate)
    assert not fake.state.calls


def test_crisis_detection_and_simultaneous_duplicate(care):
    store, fake, _, state, gate = care
    enable(store)

    class CrisisJudge(LLMJudge):
        async def judge(self, text):
            return True

    state["gateway"] = SafetyGateway([], CrisisJudge())
    assert not attempt(gate)
    state["gateway"] = SafetyGateway([])

    async def simultaneous():
        return await asyncio.gather(
            gate.send(USER, "same-work", START), gate.send(USER, "same-work", START)
        )

    assert sorted(asyncio.run(simultaneous())) == [False, True]
    assert len(fake.state.calls) == 1


def test_fence_false_after_detect_blocks_send_without_reserving(care):
    """E 路修复 3：fence 在 detect 后、reserve 前求值，假即未发送。

    fence 拦下时不得消费 reserve：撤掉 fence 后同 work 仍可正常发。
    """
    store, fake, _, state, gate = care
    enable(store)

    async def fence_no():
        return False

    assert not asyncio.run(gate.send(USER, "work-1", START, fence=fence_no))
    assert not fake.state.calls
    # reserve 未被消耗：无 fence 时同 work 可发送成功。
    assert attempt(gate)
    assert len(fake.state.calls) == 1


def test_fence_receives_state_changed_during_detect(care):
    """E 路修复 3：detect 的 await 窗口内撤回同意，fence 据实时状态拦下。"""
    store, fake, _, state, gate = care
    enable(store)

    class WithdrawingJudge(LLMJudge):
        async def judge(self, text):
            store.stop(USER)  # detect 窗口内撤回
            return False

    state["gateway"] = SafetyGateway([], WithdrawingJudge())

    async def fence():
        return store.consent_state(USER, now=state["now"]) == "active"

    assert not asyncio.run(gate.send(USER, "work-1", START, fence=fence))
    assert not fake.state.calls


def test_gateway_invoke_preflight_after_slot_blocks_action():
    """E 路修复 4（网关单元）：槽后 preflight 假 → unavailable、action 不执行。"""
    from companion.quota import SendQuota
    from companion.tools import ToolGateway

    async def scenario():
        gateway = ToolGateway(
            {"send_private_msg": {"enabled": True}}, quota=SendQuota()
        )
        acted = []

        async def action():
            acted.append(True)
            return {"status": "ok"}

        async def preflight_no():
            return False

        result = await gateway.invoke(
            "send_private_msg", action, dry_run=False, identity=USER,
            preflight=preflight_no,
        )
        assert result == {"status": "unavailable"}
        assert acted == []

        async def preflight_yes():
            return True

        result = await gateway.invoke(
            "send_private_msg", action, dry_run=False, identity=USER,
            preflight=preflight_yes,
        )
        assert result == {"status": "ok"}
        assert acted == [True]

    asyncio.run(scenario())


def test_delivery_service_forwards_preflight_and_maps_rejected():
    """E 路修复 4（投递单元）：preflight 透传给 sender，假 → rejected 回执。"""
    from companion.quota import SendQuota
    from companion.services.delivery import DeliveryService

    async def scenario():
        seen = {}

        class PreflightSender:
            quota = None

            async def send_private_msg(self, user_id, message, *, priority="normal",
                                       preflight=None):
                seen["preflight"] = preflight
                if preflight is not None and not await preflight():
                    return {"status": "unavailable"}
                return {"status": "ok"}

        service = DeliveryService(PreflightSender(), SendQuota())

        async def no():
            return False

        receipt = await service.send(USER, "x", preflight=no)
        assert receipt.outcome == "rejected"
        assert seen["preflight"] is no

    asyncio.run(scenario())


@pytest.mark.timeout(10)
def test_care_blocked_after_slot_when_consent_deleted_while_waiting(care):
    """E 路修复 4（Codex 时序复现）：关怀在普通槽上等待期间同意被删，
    释放槽后槽后 preflight 拦下、关怀零发送。

    发送端换成经真实 ToolGateway/SendQuota 的发送器（与生产
    HttpOneBotClient 同链：DeliveryService → gateway.invoke → 槽 →
    preflight → action）；普通槽先被外部占满，关怀只能排队等槽。
    """
    from companion.quota import SendQuota
    from companion.tools import ToolGateway

    store, fake, _, state, _gate = care
    enable(store)
    quota = SendQuota()
    quota.slot_wait_seconds = 30.0  # 等槽预算放大，测试靠事件驱动不靠超时
    gateway = ToolGateway({"send_private_msg": {"enabled": True}}, quota=quota)
    sent_actions = []

    class GatewaySender:
        def __init__(self):
            self.quota = quota

        async def send_private_msg(self, user_id, message, *, priority="normal",
                                   quota_token=None, preflight=None):
            async def action():
                sent_actions.append(message)
                return {"status": "ok"}

            return await gateway.invoke(
                "send_private_msg", action, dry_run=False, identity=str(user_id),
                priority=priority, quota_token=quota_token, preflight=preflight,
            )

    gate = CareSendGate(
        store,
        GatewaySender(),
        whitelist=lambda uid: uid in state["allowed"],
        current_safety=lambda uid: (state["gateway"], state["safe"], state["revision"]),
        config=state["config"],
        clock=lambda: state["now"],
    )

    async def scenario():
        assert await quota.acquire_slot(False)  # 占满普通发送槽
        try:
            task = asyncio.create_task(gate.send(USER, "work-slot", START))
            # 关怀已过 detect 与 reserve、卡在等槽：给足调度机会后仍未发。
            await asyncio.sleep(0.1)
            assert not task.done()
            assert sent_actions == []
            # 等槽期间：持久化删除同意（stop 落库）。
            store.stop(USER)
            assert store.consent_state(USER, now=state["now"]) == "none"
        finally:
            quota.release_slot(False)  # 释放槽：preflight 必须在此刻拦下
        assert not await asyncio.wait_for(task, timeout=5)
        assert sent_actions == []  # action 绝未执行：REMINDER 零发送

    asyncio.run(scenario())


def test_store_outside_repo_and_default_disabled(care):
    store, fake, sender, state, _ = care
    with pytest.raises(ValueError):
        CarePreferences(ROOT / "data" / "care.db")
    enable(store)
    gate = CareSendGate(
        store,
        sender,
        whitelist=lambda _: True,
        current_safety=lambda _: (state["gateway"], True, 0),
    )
    assert not attempt(gate)
    assert not fake.state.calls
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_unavailable_safety_judge_fails_closed(care):
    from companion.safety import JudgeUnavailable

    store, fake, _, state, gate = care
    enable(store)

    class UnavailableJudge(LLMJudge):
        async def judge(self, text):
            raise JudgeUnavailable()

    state["gateway"] = SafetyGateway([], UnavailableJudge())
    assert not attempt(gate)
    assert not fake.state.calls


def test_ambiguous_send_is_not_replayed(care, monkeypatch):
    store, fake, _, _, gate = care
    enable(store)
    original = httpx.AsyncClient

    class TimeoutHTTP(original):
        async def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("synthetic timeout")

    with monkeypatch.context() as scoped:
        scoped.setattr(httpx, "AsyncClient", TimeoutHTTP)
        assert not attempt(gate)
    assert not attempt(gate)
    assert not fake.state.calls
