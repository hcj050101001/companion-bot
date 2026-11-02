"""P0 dialogue-script tests: YAML scripts driven through the fake OneBot / fake LLM stack.

Each scenario in ``tests/scripts_p0/*.yaml`` is one deterministic conversation
script. Assertions observe only outbound sends, tool invocations, model calls
and recorded pacing delays (an injected fake sleep, never a real sleep). The
fixture builds the production-profile configuration: every read-only tool on
(current time / weather / web search with extraction), connection health on,
proactive care off. Identity-contract failures that the current code does not
yet meet are registered as KD-001 via strict xfail on those parameters only.
"""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from companion.brain import CompanionBrain, DeepSeekClient
from companion.config import load_settings
from companion.ingress import create_app
from companion.safety import ModelLLMJudge, crisis_reply
from companion.tools.read_only import ReadOnlyRegistry, ReadOnlyTool
from tests.fake_onebot import private_event
from tests.test_app import SECRET
from tests.test_current_time import registry as time_registry
from tests.test_read_only_tools import call as tool_call
from tests.test_read_only_tools import mock_model
from tests.test_read_only_tools import reply as model_reply
from tests.test_weather import adapter as weather_adapter
from tests.test_weather import registry as weather_registry

SCRIPT_DIR = Path(__file__).parent / "scripts_p0"
# Identity contract (§3.3): all three items must appear in an identity answer.
IDENTITY_NAME = "阿年"
IDENTITY_ROLE = "AI 陪伴助手"
IDENTITY_MODEL = "DeepSeek"
IDENTITY_THIRD_PARTY = "第三方模型"
LOOKUP_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 30}},
    "required": ["query"],
    "additionalProperties": False,
}


def _load_scenarios():
    scenarios = []
    for path in sorted(SCRIPT_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        scenarios.extend(document["scenarios"])
    return scenarios


def _param(scenario):
    marks = []
    if scenario.get("kd"):
        marks.append(
            pytest.mark.xfail(
                strict=True, reason=f"{scenario['kd']}: {scenario['kd_reason']}"
            )
        )
    return pytest.param(scenario, id=scenario["id"], marks=marks)


SCENARIOS = _load_scenarios()


class ScriptLLM:
    """Queued LLM stub recording prompts; serves both generate and generate_json."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []

    async def _next(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        return self.responses.pop(0)

    async def generate(self, prompt):
        return await self._next(prompt)

    async def generate_json(self, prompt):
        return await self._next(prompt)


class CountingBrain:
    """Brain stub that only counts invocations; used where the model must not run."""

    def __init__(self):
        self.calls = 0

    async def respond(self, *_args, **_kwargs):
        self.calls += 1
        return "不应被调用的生成回复"

    async def respond_multipart(self, *_args, **_kwargs):
        self.calls += 1
        return '{"bubbles": ["不应被调用的生成回复"]}'

    async def respond_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        return "不应被调用的生成回复", False


class ToolPlainBrain:
    """Brain stub for the tool-offering route returning plain text, no tool use."""

    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def respond_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        return self.text, False


class PacingRecorder:
    """Fake sleep and uniform source recording every pacing range and delay."""

    def __init__(self):
        self.delays = []
        self.ranges = []

    async def sleep(self, delay):
        self.delays.append(delay)

    def uniform(self, low, high):
        self.ranges.append((low, high))
        return 0.0 if high == 0 else 1.0


@pytest.fixture
def p0_env(tmp_path, monkeypatch):
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
            "requests_per_minute": 6,
            "llm": {"provider": "deepseek", "display_name": IDENTITY_MODEL},
            "read_only_tools": {
                "enabled": True,
                "policies": {
                    "current_time": {"enabled": True, "rate_limit": 50},
                    "weather_lookup": {"enabled": True, "rate_limit": 50},
                    "web_search": {
                        "enabled": True,
                        "rate_limit": 50,
                        "content_extraction_enabled": True,
                    },
                },
            },
            "connection_health": {"enabled": True},
            "proactive_care": {"enabled": False},
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
            return json.dumps({"bubbles": ["测试回复"]}, ensure_ascii=False)

    class SafeJudgeLLM:
        async def generate(self, _prompt):
            return "NO"

    async def no_sleep(_delay):
        pass

    alerter = FakeAlerter()
    app = create_app(
        settings,
        sender=FakeSender(),
        alerter=alerter,
        sleep=no_sleep,
        random_uniform=lambda _a, _b: 0,
        brain=CompanionBrain(settings.persona, FakeLLM()),
    )
    app.state.processor.safety.judge = ModelLLMJudge(SafeJudgeLLM())
    return SimpleNamespace(
        app=app, calls=calls, alerter=alerter, settings=settings,
        processor=app.state.processor,
    )


def _messages(env):
    return [call["message"] for call in env.calls]


def _bubbles_json(*bubbles):
    return json.dumps({"bubbles": list(bubbles)}, ensure_ascii=False)


def _assert_identity_contract(text):
    assert IDENTITY_NAME in text
    assert IDENTITY_ROLE in text
    assert IDENTITY_MODEL in text
    assert IDENTITY_THIRD_PARTY in text


def _count_tool_invocations(processor):
    counts = {}
    original = processor.read_only_registry.invoke

    async def counting(name, arguments, identity, *, user_message=""):
        counts[name] = counts.get(name, 0) + 1
        return await original(name, arguments, identity, user_message=user_message)

    processor.read_only_registry.invoke = counting
    return counts


def _handle(env, event):
    return asyncio.run(env.processor.handle(event))


def _run_crisis(scenario, env, _monkeypatch):
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    brain = CountingBrain()
    env.processor.brain = brain
    counts = _count_tool_invocations(env.processor)
    assert _handle(env, private_event(scenario["user_id"], scenario["message"]))
    expected = crisis_reply(
        env.settings.safety["helplines"], False, env.settings.safety
    )
    assert _messages(env) == [expected]
    assert len(env.calls) == scenario["expect_send_count"]
    assert brain.calls == scenario["expect_llm_calls"]
    assert sum(counts.values()) == scenario["expect_tool_calls"]
    assert len(env.alerter.items) == scenario["expect_alerts"]


def _run_identity_multipart(scenario, env, _monkeypatch):
    """Identity via the multipart route: assert the target contract directly.

    KD-001 measured behaviour on the baseline (kept here as documentation,
    deliberately NOT locked in by an equality assertion): a weak model reply
    such as 「我是 AI 助手」 is currently reviewed and sent as-is, so the
    outbound reply equals ``scenario["llm_reply"]`` verbatim. The xfail cases
    below therefore assert only the contract — the outbound reply must cover
    all three identity items — which the current behaviour fails; once P3
    fixes the check, the reply becomes the compliant fallback and these
    cases XPASS without being stuck on an equality check against the old,
    incomplete reply.
    """
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    # No enabled schemas is the ordinary route into respond_multipart, as in
    # the existing multipart ingress tests.
    env.processor.read_only_registry = ReadOnlyRegistry(enabled=False)
    llm = ScriptLLM([_bubbles_json(scenario["llm_reply"])])
    env.processor.brain = CompanionBrain(env.settings.persona, llm)
    assert _handle(env, private_event(scenario["user_id"], scenario["message"]))
    if scenario.get("expect_fallback"):
        assert _messages(env) == [env.processor.identity_fallback]
    assert len(env.calls) == scenario["expect_send_count"]
    _assert_identity_contract(_messages(env)[-1])


def _run_identity_tool(scenario, env, monkeypatch):
    """Identity via the tool route: assert the target contract directly.

    KD-001 measured behaviour on the baseline (documentation only, NOT an
    equality lock): a weak reply such as 「我是 AI 助手」, or 「这是秘密」
    for a question form the matcher does not cover, is currently sent
    verbatim as ``scenario["llm_reply"]``. The request assertion below only
    proves the tool route was actually offered; the pass/fail criterion is
    the identity contract on the outbound reply, so a P3 fix that swaps the
    reply for the compliant disclosure turns these cases into XPASS.
    """
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    env.processor.brain = CompanionBrain(env.settings.persona, DeepSeekClient())
    requests = mock_model(monkeypatch, [model_reply(scenario["llm_reply"])])
    assert _handle(env, private_event(scenario["user_id"], scenario["message"]))
    # Route proof only: the tool route was offered (one request, 3 tools).
    assert len(requests) == 1 and len(requests[0]["tools"]) == 3
    assert len(env.calls) == scenario["expect_send_count"]
    _assert_identity_contract(_messages(env)[-1])


def _run_forget_sequence(scenario, env, _monkeypatch):
    user = scenario["user_id"]
    asyncio.run(env.processor.conversation.mark_onboarded(user))
    env.processor.read_only_registry = ReadOnlyRegistry(enabled=False)
    llm = ScriptLLM([
        _bubbles_json(scenario["seed_reply"]),
        _bubbles_json(scenario["followup_reply"]),
    ])
    env.processor.brain = CompanionBrain(env.settings.persona, llm)
    assert _handle(env, private_event(user, scenario["seed_message"]))
    assert scenario["forgotten_token"] in str(
        asyncio.run(env.processor.conversation.get_history(user))
    )
    assert _handle(env, private_event(user, scenario["forget_message"]))
    assert _messages(env)[-1] == scenario["forget_reply"]
    assert asyncio.run(env.processor.conversation.get_history(user)) == []
    assert _handle(env, private_event(user, scenario["followup_message"]))
    assert _messages(env)[-1] == scenario["followup_reply"]
    assert llm.calls == 2  # the forget turn itself never reaches the model
    assert scenario["forgotten_token"] not in llm.prompts[-1]
    assert scenario["seed_message"] not in llm.prompts[-1]


def _run_forget_before_crisis(scenario, env, _monkeypatch):
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    brain = CountingBrain()
    env.processor.brain = brain
    assert _handle(env, private_event(scenario["user_id"], scenario["message"]))
    assert _messages(env) == [scenario["forget_reply"]]
    crisis = crisis_reply(env.settings.safety["helplines"], False, env.settings.safety)
    assert crisis not in _messages(env)
    assert len(env.alerter.items) == scenario["expect_alerts"]
    assert brain.calls == scenario["expect_llm_calls"]


def _run_multibubble(scenario, env, _monkeypatch):
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    # The plain-text split contract for single-newline lines lives on the
    # tool-offering route, as in the existing multipart ingress tests.
    async def action(query):
        return "synthetic-tool-result"

    registry = ReadOnlyRegistry(
        enabled=True, policies={"lookup": {"enabled": True, "rate_limit": 50}}
    )
    registry.register(ReadOnlyTool("lookup", "Synthetic lookup", LOOKUP_SCHEMA, action))
    env.processor.read_only_registry = registry
    env.processor.brain = ToolPlainBrain(scenario["llm_text"])
    pacing = PacingRecorder()
    env.processor.sleep = pacing.sleep
    env.processor.random_uniform = pacing.uniform
    assert _handle(env, private_event(scenario["user_id"], scenario["message"]))
    assert _messages(env) == scenario["expected_bubbles"]
    if scenario["explicit"]:
        # Round 7 contract (production 09b621c, ported at cutover): an
        # explicit one-by-one script may send up to MAX_ONE_BY_ONE_BUBBLES
        # bubbles; pacing is (0, 0) for the first, then 0.8–1.5 per gap.
        from companion.multipart import MAX_ONE_BY_ONE_BUBBLES

        assert len(env.calls) <= MAX_ONE_BY_ONE_BUBBLES
        assert pacing.ranges == [(0, 0)] + [(0.8, 1.5)] * (
            len(scenario["expected_bubbles"]) - 1
        )
        assert all(0.8 <= delay <= 1.5 for delay in pacing.delays[1:])
    else:
        assert len(env.calls) == 1
        assert pacing.ranges == [(0, 0)]


def _run_rate_limit(scenario, env, _monkeypatch):
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    env.processor.read_only_registry = ReadOnlyRegistry(enabled=False)
    llm = ScriptLLM([_bubbles_json(reply) for reply in scenario["replies"]])
    env.processor.brain = CompanionBrain(env.settings.persona, llm)
    results = [
        _handle(env, private_event(scenario["user_id"], text))
        for text in scenario["messages"]
    ]
    assert results == [True] * scenario["expect_sent_count"] + [False]
    assert _messages(env) == scenario["replies"][: scenario["expect_sent_count"]]
    assert llm.calls == scenario["expect_llm_calls"]


def _run_tool_budget(scenario, env, monkeypatch):
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    seen = []

    async def action(query):
        seen.append(query)
        return "synthetic-tool-result"

    registry = ReadOnlyRegistry(
        enabled=True, policies={"lookup": {"enabled": True, "rate_limit": 50}}
    )
    registry.register(ReadOnlyTool("lookup", "Synthetic lookup", LOOKUP_SCHEMA, action))
    env.processor.read_only_registry = registry
    env.processor.brain = CompanionBrain(env.settings.persona, DeepSeekClient())
    induced = [tool_call(str(i)) for i in range(scenario["induced_calls"])]
    requests = mock_model(monkeypatch, [
        model_reply(calls=induced),
        model_reply(scenario["synthesis_reply"]),
    ])
    assert _handle(env, private_event(scenario["user_id"], scenario["message"]))
    assert len(seen) == scenario["expect_tool_calls"]
    assert len(seen) <= 3
    assert len(requests) == 2 and requests[1]["tool_choice"] == "none"
    assert _messages(env) == [scenario["synthesis_reply"]]
    assert len(_messages(env)[-1]) <= scenario["max_reply_chars"]


def _run_weather_direct(scenario, env, monkeypatch):
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    instance, provider_requests = weather_adapter()
    env.processor.read_only_registry = weather_registry(instance)
    env.processor.brain = CompanionBrain(env.settings.persona, DeepSeekClient())
    requests = mock_model(monkeypatch, [
        model_reply(calls=[tool_call(
            "w", name="weather_lookup",
            arguments='{"city":"Testville","day":"today"}',
        )]),
    ])
    assert _handle(env, private_event(scenario["user_id"], scenario["message"]))
    assert len(env.calls) == scenario["expect_send_count"]
    assert len(requests) == scenario["expect_model_requests"]
    assert len(provider_requests) == scenario["expect_provider_requests"]
    message = _messages(env)[-1]
    assert "Open-Meteo" in message and "来源：Open-Meteo" in message
    assert "CC BY 4.0" in message and scenario["city"] in message
    assert asyncio.run(env.processor.conversation.get_history(scenario["user_id"])) == []


def _run_time_direct(scenario, env, _monkeypatch):
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    fixed = datetime(2026, 10, 30, 4, 34, 56, tzinfo=timezone.utc)
    env.processor.read_only_registry = time_registry(lambda: fixed)
    brain = CountingBrain()
    env.processor.brain = brain
    assert _handle(env, private_event(scenario["user_id"], scenario["message"]))
    assert _messages(env) == [scenario["expected_reply"]]
    assert len(env.calls) == scenario["expect_send_count"]
    assert brain.calls == scenario["expect_llm_calls"]
    assert asyncio.run(env.processor.conversation.get_history(scenario["user_id"])) == []


def _run_media_only(scenario, env, _monkeypatch):
    asyncio.run(env.processor.conversation.mark_onboarded(scenario["user_id"]))
    brain = CountingBrain()
    env.processor.brain = brain
    event = private_event(scenario["user_id"], "")
    event["message"] = [{"type": "image", "data": {"file": "synthetic"}}]
    event["raw_message"] = ""
    assert _handle(env, event)
    assert _messages(env) == [scenario["expect_reply"]]
    assert len(env.calls) == scenario["expect_send_count"]
    assert brain.calls == scenario["expect_llm_calls"]


def _run_group_silent(scenario, env, _monkeypatch):
    event = {
        "post_type": "message",
        "message_type": "group",
        "user_id": scenario["user_id"],
        "group_id": scenario["group_id"],
        "message": scenario["message"],
        "raw_message": scenario["message"],
        "message_id": "p0-group-1",
    }
    assert not _handle(env, event)
    assert env.calls == []
    assert len(env.calls) == scenario["expect_send_count"]


_RUNNERS = {
    "crisis": _run_crisis,
    "identity_multipart": _run_identity_multipart,
    "identity_tool": _run_identity_tool,
    "forget_sequence": _run_forget_sequence,
    "forget_before_crisis": _run_forget_before_crisis,
    "multibubble": _run_multibubble,
    "rate_limit": _run_rate_limit,
    "tool_budget": _run_tool_budget,
    "weather_direct": _run_weather_direct,
    "time_direct": _run_time_direct,
    "media_only": _run_media_only,
    "group_silent": _run_group_silent,
}


@pytest.mark.timeout(10)
@pytest.mark.parametrize("scenario", [_param(s) for s in SCENARIOS])
def test_dialogue_script_p0(scenario, p0_env, monkeypatch):
    _RUNNERS[scenario["kind"]](scenario, p0_env, monkeypatch)


@pytest.mark.timeout(10)
def test_p0_production_profile(p0_env):
    app_config = p0_env.settings.app
    assert app_config["read_only_tools"]["enabled"] is True
    policies = app_config["read_only_tools"]["policies"]
    assert policies["current_time"]["enabled"] is True
    assert policies["weather_lookup"]["enabled"] is True
    assert policies["web_search"]["enabled"] is True
    assert policies["web_search"]["content_extraction_enabled"] is True
    assert app_config["connection_health"]["enabled"] is True
    assert app_config["proactive_care"]["enabled"] is False
    registry = p0_env.processor.read_only_registry
    assert {schema["function"]["name"] for schema in registry.schemas()} == {
        "current_time",
        "weather_lookup",
        "web_search",
    }
    # The fixed fallback itself must already satisfy the identity contract.
    _assert_identity_contract(p0_env.processor.identity_fallback)
