"""Synthetic clock and ingress coverage; no external services or personal data."""

import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from companion.brain import TIME_SAFETY_INSTRUCTION, CompanionBrain, DeepSeekClient
from companion.ingress import RateLimiter
from companion.safety import crisis_reply
from companion.tools.current_time import (
    SCHEMA,
    UNAVAILABLE_REPLY,
    current_time_tool,
    format_current_time,
    is_current_time_request,
)
from companion.tools.plugins import CurrentTimePlugin, PluginRegistry
from companion.tools.read_only import UNAVAILABLE, ReadOnlyRegistry, ReadOnlyTool
from tests.fake_onebot import private_event
from tests.test_read_only_tools import call, mock_model, reply

pytest_plugins = ["tests.test_app"]


def registry(clock=None, *, enabled=True, policy=True, zone_factory=None):
    instance = ReadOnlyRegistry(enabled=enabled, policies={"current_time": {"enabled": policy}})
    PluginRegistry(instance).register_plugin(CurrentTimePlugin(clock, zone_factory))
    return instance


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def invoke(instance, args="{}", identity="synthetic", message="现在几点"):
    return asyncio.run(instance.invoke("current_time", args, identity, user_message=message))


@pytest.mark.parametrize("value,expected", [
    ("2024-02-28T16:00:00.987654", ("2024-02-29", "Thursday", "00:00:00", "night")),
    ("2024-02-29T15:59:59.999999", ("2024-02-29", "Thursday", "23:59:59", "evening")),
    ("2026-10-30T00:00:01.999999", ("2026-10-30", "Friday", "08:00:01", "morning")),
    ("2026-10-29T21:59:59", ("2026-10-30", "Friday", "05:59:59", "night")),
    ("2026-10-29T22:00:00", ("2026-10-30", "Friday", "06:00:00", "morning")),
    ("2026-10-30T03:59:59", ("2026-10-30", "Friday", "11:59:59", "morning")),
    ("2026-10-30T04:00:00", ("2026-10-30", "Friday", "12:00:00", "afternoon")),
    ("2026-10-30T09:59:59", ("2026-10-30", "Friday", "17:59:59", "afternoon")),
    ("2026-10-30T10:00:00", ("2026-10-30", "Friday", "18:00:00", "evening")),
])
def test_clock_contract(value, expected):
    calls = []

    def clock():
        calls.append(1)
        return instant(value)

    result = invoke(registry(clock))
    data_string = json.loads(result)["data"]
    data = json.loads(data_string)
    assert tuple(data) == ("date", "weekday", "time", "time_of_day", "timezone", "utc_offset")
    assert tuple(data[k] for k in ("date", "weekday", "time", "time_of_day")) == expected
    assert data["timezone"] == "Asia/Shanghai" and data["utc_offset"] == "+08:00"
    assert data_string == json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    assert format_current_time(result) == (
        f'{expected[0]} {expected[1]} {expected[2]} '
        f'({expected[3]}, Asia/Shanghai, UTC+08:00)'
    )
    assert len(result) < 2000 and calls == [1]


@pytest.mark.parametrize("enabled,policy", [(False, False), (True, False), (False, True),
                                             (True, True), (1, True), (True, 1)])
def test_flags(enabled, policy):
    reg = registry(lambda: instant("2026-10-30T00:00:00"), enabled=enabled, policy=policy)
    on = enabled is True and policy is True
    assert bool(reg.schemas()) == on
    assert (invoke(reg) != UNAVAILABLE) == on
    if on:
        assert reg.schemas()[0]["function"]["parameters"] == SCHEMA


@pytest.mark.parametrize("args", ["{", "[]", "null", '{"x":1}', '{"x":1,"x":2}',
                                  '{"timezone":"Asia/Shanghai"}', '{"offset":"+08:00"}',
                                  '{"date":"2026-01-01"}', '{"user_id":"synthetic"}',
                                  '{"recipient":"synthetic"}', '{"identity":"synthetic"}'])
def test_invalid_arguments(args):
    reg = registry(lambda: pytest.fail("invalid arguments reached clock"))
    assert invoke(reg, args) == UNAVAILABLE


@pytest.mark.parametrize("clock,zone_factory", [
    (lambda: None, ZoneInfo),
    (lambda: datetime(2026, 1, 1), ZoneInfo),
    (lambda: (_ for _ in ()).throw(OSError("synthetic private detail")), ZoneInfo),
    (lambda: instant("2026-01-01T00:00:00"),
     lambda _zone: (_ for _ in ()).throw(ZoneInfoNotFoundError("missing"))),
    (lambda: instant("2026-01-01T00:00:00"), lambda _zone: timezone.utc),
])
def test_clock_and_tz_failure(clock, zone_factory, caplog):
    result = invoke(registry(clock, zone_factory=zone_factory))
    assert result == UNAVAILABLE and format_current_time(result) == UNAVAILABLE_REPLY
    assert "synthetic private detail" not in caplog.text


def test_rate_limit_per_identity_and_redacted_logs(caplog):
    reg = registry(lambda: instant("2026-10-30T00:00:00"))
    for _ in range(6):
        assert invoke(reg, identity="synthetic-identity") != UNAVAILABLE
    assert invoke(reg, identity="synthetic-identity") == UNAVAILABLE
    assert invoke(reg, identity="another-synthetic") != UNAVAILABLE
    assert "synthetic-identity" not in caplog.text
    assert "现在几点" not in caplog.text


def test_result_cap_fails_closed():
    async def oversized():
        return '"' * 2000

    reg = ReadOnlyRegistry(enabled=True, policies={"current_time": {"enabled": True}})
    reg.register(ReadOnlyTool("current_time", "synthetic", SCHEMA, oversized))
    assert invoke(reg) == UNAVAILABLE


def test_shared_queue_and_timeout(monkeypatch):
    import companion.tools.read_only as read_only

    real_wait_for = asyncio.wait_for
    timeouts = []

    async def short_wait(awaitable, timeout):
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=0.2 if len(timeouts) == 1 else 0.02)

    monkeypatch.setattr(read_only.asyncio, "wait_for", short_wait)
    active = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocker():
        active.append("start")
        started.set()
        await release.wait()
        active.append("end")
        return "ok"

    async def exercise():
        reg = registry(lambda: instant("2026-10-30T00:00:00"))
        reg.gateway.policies["blocker"] = {"enabled": True}
        reg.register(ReadOnlyTool("blocker", "synthetic", SCHEMA, blocker))
        first = asyncio.create_task(reg.invoke("blocker", "{}", "a"))
        await started.wait()
        second = await reg.invoke("current_time", "{}", "b", user_message="现在几点")
        release.set()
        await first
        return second

    assert asyncio.run(exercise()) == UNAVAILABLE
    assert active == ["start", "end"] and timeouts == [10, 10]


def test_no_network(monkeypatch):
    import socket

    def fail(*_args, **_kwargs):
        pytest.fail("network access")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket.socket, "connect_ex", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert invoke(registry(lambda: instant("2026-10-30T00:00:00"))) != UNAVAILABLE


@pytest.mark.parametrize("message", ["现在几点", "现在几点了？", "现在几点钟", "现在是什么时间",
                                       "现在的时间", "当前时间", "What time is it now?",
                                       "what's the current time", "current time"])
def test_supported_requests(message):
    assert is_current_time_request(message)


@pytest.mark.parametrize("message", ["几点", "现在几点在伦敦", "明天几点", "现在几点和天气",
                                       "what time", "current time in Paris", "现在几点 想自杀"])
def test_ambiguous_requests_are_not_intercepted(message):
    assert not is_current_time_request(message)
    assert invoke(registry(lambda: pytest.fail("ambiguous call")), message=message) == UNAVAILABLE


@pytest.mark.parametrize("bad", [UNAVAILABLE, '{}', '{"status":"ok","data":"{}"}',
                                 '{"status":"ok","data":"{}","data":"{}"}'])
def test_formatter_rejects_bad_results(bad):
    assert format_current_time(bad) == UNAVAILABLE_REPLY


def test_model_choice_and_refusal(monkeypatch):
    reg = registry(lambda: instant("2026-10-30T04:34:56"))
    mock_model(monkeypatch, [reply(calls=[call("x", "current_time", "{}")]),
                             reply("synthetic ordinary reply")])
    result = asyncio.run(DeepSeekClient().generate_with_tools(
        "synthetic", reg, "synthetic", user_message="现在几点"))
    assert result == ("2026-10-30 Friday 12:34:56 (afternoon, Asia/Shanghai, UTC+08:00)", True)
    assert asyncio.run(DeepSeekClient().generate_with_tools(
        "synthetic", reg, "synthetic", user_message="ambiguous")) == (
            "synthetic ordinary reply", False)


def test_ingress_routes_safety_memory_and_disabled_parity(setup_app):
    app, calls, _, settings = setup_app
    processor = app.state.processor
    samples = []
    processor.read_only_registry = registry(lambda: samples.append(1) or
                                             instant("2026-10-30T04:34:56"))
    processor.limiter = RateLimiter(100)

    class FabricatingModel:
        async def generate(self, prompt):
            assert TIME_SAFETY_INSTRUCTION in prompt
            return "12:34:56"

        async def generate_json(self, prompt):
            assert TIME_SAFETY_INSTRUCTION in prompt
            return '{"bubbles": ["12:34:56"]}'

    processor.brain = CompanionBrain(settings.persona, FabricatingModel())

    async def exercise():
        await processor.handle(private_event(user_id="10002", text="现在几点"))
        assert len(calls) == 2 and not samples
        await processor.handle(private_event(user_id="10002", text="现在几点"))
        assert samples == [1]
        samples.clear()
        await processor.handle(private_event(text="现在几点"))  # onboarding uses normal path
        assert not samples
        assert calls[-1]["message"] == "12:34:56"
        (await processor.conversation.forget("10001"))
        await processor.handle(private_event(text="现在几点"))
        assert samples == [1]
        assert calls[-1]["message"] == (
            "2026-10-30 Friday 12:34:56 (afternoon, Asia/Shanghai, UTC+08:00)")
        assert (await processor.conversation.get_history("10001")) == []
        await processor.handle(private_event(text="忘掉我"))
        await processor.handle(private_event(text="想自杀 现在几点"))
        assert calls[-1]["message"] == crisis_reply(settings.safety["helplines"], False,
                                                       settings.safety)
        assert samples == [1]
        processor.read_only_registry.enabled = False
        await processor.handle(private_event(text="现在几点"))
        assert calls[-1]["message"] == "12:34:56"  # Demonstrates feature-off residual.
        assert samples == [1]
        processor.read_only_registry.enabled = True
        processor.read_only_registry._tools["current_time"] = current_time_tool(lambda: None)
        await processor.handle(private_event(text="现在几点"))
        assert calls[-1]["message"] == UNAVAILABLE_REPLY

    asyncio.run(exercise())
    logs = processor.log_path.read_text()
    assert "现在几点" not in logs and "10001" not in logs and "12:34:56" not in logs


def test_formatter_rejects_inconsistent_data():
    result = invoke(registry(lambda: instant("2026-10-30T04:34:56")))
    wrapper = json.loads(result)
    original = json.loads(wrapper["data"])
    for key, value in [("weekday", "Monday"), ("time", "25:00:00"),
                       ("time_of_day", "night"), ("timezone", "UTC"),
                       ("utc_offset", "+25:00"), ("date", "2026-02-30")]:
        changed = {**original, key: value}
        assert format_current_time(json.dumps({"status": "ok", "data": json.dumps(changed)})) == (
            UNAVAILABLE_REPLY)


def test_time_call_obeys_model_tool_budget(monkeypatch):
    count = []
    reg = registry(lambda: count.append(1) or instant("2026-10-30T04:34:56"))

    async def action():
        return "synthetic"

    reg.gateway.policies["other"] = {"enabled": True}
    reg.register(ReadOnlyTool("other", "synthetic", SCHEMA, action))
    mock_model(monkeypatch, [reply(calls=[call(str(i), "other", "{}") for i in range(3)] +
                                  [call("time", "current_time", "{}")])])
    assert asyncio.run(DeepSeekClient().generate_with_tools(
        "synthetic", reg, "synthetic", user_message="现在几点")) == (UNAVAILABLE_REPLY, True)
    assert count == []


def test_disabled_model_time_call_retains_unknown_tool_behavior(monkeypatch):
    reg = registry(enabled=True, policy=False)

    async def action():
        return "synthetic"

    reg.gateway.policies["other"] = {"enabled": True}
    reg.register(ReadOnlyTool("other", "synthetic", SCHEMA, action))
    requests = mock_model(monkeypatch, [reply(calls=[call("time", "current_time", "{}")]),
                                       reply("ordinary")])
    assert asyncio.run(DeepSeekClient().generate_with_tools(
        "synthetic", reg, "synthetic", user_message="现在几点")) == ("ordinary", True)
    assert requests[1]["messages"][-1]["content"] == UNAVAILABLE


@pytest.mark.parametrize("message", [
    "几点", "what time", "现在几点在伦敦", "current time in Paris",
])
def test_ambiguous_requests_clarify_without_clock_or_model(setup_app, message):
    from companion.tools.current_time import CLARIFY_REPLY

    app, calls, _, settings = setup_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.read_only_registry = registry(lambda: pytest.fail("ambiguous clock call"))

    class FabricatingModel:
        async def generate(self, _prompt):
            pytest.fail("ambiguous model call")

    processor.brain = CompanionBrain(settings.persona, FabricatingModel())
    assert asyncio.run(processor.handle(private_event(text=message)))
    assert calls[-1]["message"] == CLARIFY_REPLY


def test_eligible_route_rate_limit_returns_unavailable_without_guess(setup_app):
    app, calls, _, _ = setup_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter = RateLimiter(100)
    processor.read_only_registry = registry(lambda: instant("2026-10-30T04:34:56"))

    async def exercise():
        for _ in range(7):
            assert await processor.handle(private_event(text="现在几点"))

    asyncio.run(exercise())
    assert calls[-1]["message"] == UNAVAILABLE_REPLY
    assert asyncio.run(processor.conversation.get_history("10001")) == []
