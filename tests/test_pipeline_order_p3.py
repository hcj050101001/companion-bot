"""P3 pipeline stage-order contract tests (§5.3: order *is* the contract).

- ``companion.pipeline.STAGE_ORDER`` (and the ``companion.ingress`` module
  attribute the processor is built from) must equal the fixed eleven-stage
  tuple, item for item.
- The order is behaviourally load-bearing, proven by a measured swap: with
  the inbound rate limiter (6/min/user) exhausted, a keyword crisis as the
  seventh message still reaches the user in the contract order because
  SafetyJudge runs *before* InboundRateLimit. Swapping those two stages in
  ``companion.ingress.STAGE_ORDER`` before building a fresh app makes the
  very same crisis message die silently in the rate limiter instead — the
  fixed crisis copy never appears. Both assertions are written out so the
  test is red on any reordering, not just on a broken crisis path.

The app fixture mirrors ``tests.test_app.setup_app`` (same allowlist, safety
table and fake LLM/judge), so the two runs differ only in stage order.
Only fake senders are used; no real QQ message is ever sent.
"""

import asyncio
import time
from dataclasses import replace

import pytest
import yaml

import companion.ingress as ingress
from companion.brain import CompanionBrain
from companion.config import load_settings
from companion.ingress import create_app
from companion.pipeline import STAGE_ORDER as PIPELINE_STAGE_ORDER
from companion.safety import ModelLLMJudge, crisis_reply
from tests import test_app
from tests.test_adapter_contract_p0 import _post, fake_event, signed_step

SECRET = test_app.SECRET
USER = "10001"

EXPECTED_STAGE_ORDER = (
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


def build_app(tmp_path, monkeypatch):
    """setup_app-shaped fixture; STAGE_ORDER is read at construction time."""
    monkeypatch.setenv("ONEBOT_SECRET", SECRET)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_QQ", raising=False)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    base = load_settings()
    settings = replace(
        base,
        onebot_secret=SECRET,
        dev=False,
        root=tmp_path,
        app={
            **base.app,
            "allowed_private_user_ids": ["10001"],
            "sqlite_path": "memory.db",
            "logs_dir": "logs",
            "response_delay_seconds": {"min": 0, "max": 0},
            # The swap scenario is calibrated to the 6/min inbound limiter.
            "requests_per_minute": 6,
        },
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
    calls = []

    class FakeSender:
        async def send_private_msg(self, user_id, message, *, priority="normal"):
            calls.append({"user_id": int(user_id), "message": message})
            return {"status": "ok"}

        async def send_alert_private_msg(self, user_id, message):
            return await self.send_private_msg(user_id, message)

    class FakeAlerter:
        def __init__(self):
            self.items = []

        async def alert(self, user_id, reason, summary="sensitive text omitted"):
            self.items.append((user_id, reason, summary))

    class FakeLLM:
        async def generate(self, _prompt):
            return "测试回复"

        async def generate_json(self, _prompt):
            return '{"bubbles": ["测试回复"]}'

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
    return app, calls, settings


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def exercise_rate_limit_then_crisis(
    app, calls, settings, mid_prefix: str, *, expect_crisis: bool
):
    """Six ordinary messages (exhausting the limiter), then one crisis.

    Runs with the consumer live, posting strictly sequentially: each post is
    only issued after the previous message's sends have been observed, so
    the limiter consumption order is deterministic. Returns the recorded
    sends after the crisis message has fully settled.
    """
    crisis = crisis_reply(settings.safety["helplines"], False, settings.safety)

    async def scenario():
        async def post(event) -> int:
            body, headers = signed_step(event)
            response = await _post(app, body, headers)
            return response.status_code

        for index in range(6):
            event = fake_event(
                f"{mid_prefix}-ord-{index}", user=USER, text=f"普通消息{index}"
            )
            assert await post(event) == 204
            # First message sends onboarding + reply (2 sends); each further
            # ordinary message sends one reply. Wait for the sends before
            # posting the next message so limiter slots fill in order.
            expected_sends = index + 2
            assert await wait_until(lambda: len(calls) >= expected_sends), (
                f"ordinary message {index} did not produce {expected_sends} sends"
            )
        assert len(calls) == 7
        crisis_event = fake_event(f"{mid_prefix}-crisis", user=USER, text="想自杀")
        assert await post(crisis_event) == 204
        if expect_crisis:
            # Contract order: the crisis is judged before the limiter is
            # consulted, so the exhausted limiter cannot swallow it.
            assert await wait_until(lambda: crisis in [c["message"] for c in calls]), (
                "fixed crisis copy was not sent in the contract stage order"
            )
        else:
            # Swapped order: the limiter runs first and drops the crisis
            # silently. Give processing ample time to settle, then prove
            # the crisis copy never appeared and nothing else was sent.
            await asyncio.sleep(0.8)
            await asyncio.wait_for(app.state.processor.wait_idle(), timeout=5)
        return list(calls), crisis

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


@pytest.mark.timeout(10)
def test_stage_order_matches_contract_tuple():
    assert PIPELINE_STAGE_ORDER == EXPECTED_STAGE_ORDER
    assert ingress.STAGE_ORDER == EXPECTED_STAGE_ORDER
    assert len(EXPECTED_STAGE_ORDER) == 11


@pytest.mark.timeout(10)
def test_contract_order_crisis_bypasses_exhausted_rate_limit(tmp_path, monkeypatch):
    app, calls, settings = build_app(tmp_path, monkeypatch)
    sent, crisis = exercise_rate_limit_then_crisis(
        app, calls, settings, "order-ok", expect_crisis=True
    )
    # 6 ordinary messages (onboarding + 6 replies = 7 sends) and, despite
    # the exhausted limiter, the fixed crisis copy as the eighth send.
    assert [call["message"] for call in sent].count(crisis) == 1
    assert sent[-1]["message"] == crisis
    assert len(sent) == 8


@pytest.mark.timeout(10)
def test_swapping_safety_and_rate_limit_stages_drops_crisis(tmp_path, monkeypatch):
    swapped = list(EXPECTED_STAGE_ORDER)
    safety_at = swapped.index("SafetyJudge")
    limiter_at = swapped.index("InboundRateLimit")
    swapped[safety_at], swapped[limiter_at] = swapped[limiter_at], swapped[safety_at]
    # The processor is built from the ingress module attribute: swapping it
    # *before* construction must swap the stages the new app actually runs.
    monkeypatch.setattr(ingress, "STAGE_ORDER", tuple(swapped))

    app, calls, settings = build_app(tmp_path, monkeypatch)
    sent, crisis = exercise_rate_limit_then_crisis(
        app, calls, settings, "order-swap", expect_crisis=False
    )
    # Same six ordinary messages go through (7 sends), but the seventh —
    # the crisis text — is now rate-limited before SafetyJudge ever sees
    # it: no fixed crisis copy, and no other reply either.
    assert crisis not in [call["message"] for call in sent]
    assert len(sent) == 7
