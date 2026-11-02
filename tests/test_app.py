"""W1 integration and configuration regression tests."""

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace

import httpx
import pytest
import yaml

from companion.brain import (
    FALLBACK_REPLY,
    CodexClient,
    DeepSeekClient,
    OpenAICompatibleClient,
    create_llm_client,
)
from companion.config import load_settings
from companion.ingress import RateLimiter, create_app, verify_signature
from companion.memory import ConversationStore, Database
from companion.safety import (
    EmailAlerter,
    JsonlAlerter,
    ModelLLMJudge,
    SafetyGateway,
    crisis_reply,
)
from companion.tools import HttpOneBotClient
from tests.fake_onebot import private_event

SECRET = "fixed-test-only-secret"


@pytest.fixture
def setup_app(tmp_path, monkeypatch):
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
            return json.dumps({"bubbles": ["测试回复"]}, ensure_ascii=False)

    async def no_sleep(_delay):
        pass

    from companion.brain import CompanionBrain

    class SafeJudgeLLM:
        async def generate(self, _prompt):
            return "NO"

    alerter = FakeAlerter()
    brain = CompanionBrain(settings.persona, FakeLLM())
    app = create_app(
        settings,
        sender=FakeSender(),
        alerter=alerter,
        sleep=no_sleep,
        random_uniform=lambda _a, _b: 0,
        brain=brain,
    )
    # 测试默认不把无 key 的 DeepSeek FALLBACK 当成 E2b 疑似危机
    app.state.processor.safety.judge = ModelLLMJudge(SafeJudgeLLM())
    return app, calls, alerter, settings


def signed(body, secret=SECRET):
    return "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def encoded_event(user="10001", text="今天有点累"):
    event = private_event(user, text)
    event["message_id"] = f"{user}-{text}"
    return json.dumps(event, ensure_ascii=False).encode()


async def drain(app):
    await app.state.event_bus.join()
    await app.state.processor.wait_idle()


async def request_with_worker(app, request):
    consumer = asyncio.create_task(app.state.event_consumer())
    try:
        result = await request()
        await drain(app)
        return result
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.timeout(10)
def test_signature_and_injected_event(setup_app):
    app, calls, _, _ = setup_app
    body = encoded_event()
    assert verify_signature(body, signed(body), SECRET)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            valid = await client.post(
                "/onebot/event", content=body, headers={"X-Signature": signed(body)}
            )
            invalid = await client.post(
                "/onebot/event", content=body, headers={"X-Signature": "sha1=bad"}
            )
            return valid, invalid

    valid, invalid = asyncio.run(request_with_worker(app, run))
    assert valid.status_code == 204 and invalid.status_code == 401
    assert len(calls) == 2
    assert "阿年" in calls[0]["message"]
    assert "忘掉我" in calls[0]["message"]
    assert "第三方模型" not in calls[0]["message"]
    assert calls[1]["message"] == "测试回复"


@pytest.mark.timeout(10)
def test_end_to_end_through_fake_onebot_http_service(setup_app, monkeypatch):
    """Exercise event ingress and OneBot HTTP sending through fake services."""
    from tests.fake_onebot import make_fake_onebot

    app, _, _, _ = setup_app
    fake = make_fake_onebot(SECRET)

    async def run():
        app_transport = httpx.ASGITransport(app=app)
        fake_transport = httpx.ASGITransport(app=fake)

        async def transport_for_host(request):
            return await fake_transport.handle_async_request(request)

        original = httpx.AsyncClient

        class FakeAsyncClient(original):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(transport_for_host)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
        app.state.processor.sender = HttpOneBotClient("http://fake-onebot")
        async with original(transport=app_transport, base_url="http://companion") as ingress:
            body = encoded_event()
            response = await ingress.post(
                "/onebot/event", content=body, headers={"X-Signature": signed(body)}
            )
            await drain(app)
        return response

    response = asyncio.run(request_with_worker(app, run))
    assert response.status_code == 204
    assert fake.state.calls[-1] == {"user_id": 10001, "message": "测试回复"}
    assert len(fake.state.calls) == 2


@pytest.mark.parametrize("user", ["10001", "10002"])
@pytest.mark.timeout(10)
def test_private_admission_onboarding_and_reply(setup_app, user):
    app, calls, _, settings = setup_app
    original = "synthetic first message"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for message in (original, "synthetic second message"):
                body = encoded_event(user, message)
                response = await client.post(
                    "/onebot/event", content=body, headers={"X-Signature": signed(body)}
                )
                assert response.status_code == 204
                await drain(app)

    asyncio.run(request_with_worker(app, run))
    assert len(calls) == 3
    assert "阿年" in calls[0]["message"]
    assert [call["message"] for call in calls[1:]] == ["测试回复", "测试回复"]
    assert all(call["user_id"] == int(user) for call in calls)
    assert asyncio.run(app.state.processor.conversation.is_onboarded(user))
    log = (settings.root / "logs/events.jsonl").read_text()
    assert original not in log and user not in log and '"rejected"' not in log


def test_non_whitelisted_crisis_bypasses_ordinary_tools(setup_app, monkeypatch):
    app, calls, alerter, settings = setup_app
    processor = app.state.processor
    user = "10002"  # Synthetic identity outside the fixture's allowlist.
    asyncio.run(processor.conversation.mark_onboarded(user))
    processor.read_only_registry.enabled = True
    processor.read_only_registry.gateway.policies["current_time"] = {"enabled": True}
    assert processor.read_only_registry.is_enabled("current_time")

    async def unexpected(*args, **kwargs):
        pytest.fail("crisis reached ordinary response handling")

    monkeypatch.setattr(processor.brain, "respond", unexpected)
    monkeypatch.setattr(processor.brain, "respond_with_tools", unexpected)
    assert asyncio.run(processor.handle(private_event(user, "想自杀 现在几点")))
    assert calls[-1]["message"] == crisis_reply(
        settings.safety["helplines"], False, settings.safety
    )
    assert len(calls) == 1 and len(alerter.items) == 1


def test_non_whitelisted_identity_rate_limit(setup_app):
    app, calls, _, _ = setup_app
    processor = app.state.processor
    processor.limiter = RateLimiter(limit=1)

    async def run():
        assert await processor.handle(private_event("10002", "synthetic first"))
        assert not await processor.handle(private_event("10002", "synthetic second"))
        assert await processor.handle(private_event("10001", "synthetic other identity"))

    asyncio.run(run())
    assert len(calls) == 4  # onboarding and ordinary reply for each identity
    records = [json.loads(line) for line in processor.log_path.read_text().splitlines()]
    assert [record["event_type"] for record in records] == ["message", "rate_limited", "message"]


@pytest.mark.timeout(10)
def test_crisis_modes_redaction_and_only_crisis_triggers(setup_app, monkeypatch):
    app, calls, alerter, _ = setup_app
    monkeypatch.setenv("ALERT_QQ", "10002")
    app.state.processor.alert_qq = "10002"
    app.state.processor.settings.app.setdefault("tools", {}).setdefault(
        "alert_private_msg", {}
    )["enabled"] = True
    private = "MY_PRIVATE_CRISIS_TEXT"
    body = encoded_event("10001", "活着没意思 " + private)

    async def run(payload):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/onebot/event", content=payload, headers={"X-Signature": signed(payload)}
            )

    async def send_both():
        await run(encoded_event(text="普通消息"))
        await drain(app)
        await run(body)
        await drain(app)
        body2 = encoded_event("10001", "活着没意思 " + private + " second")
        await run(body2)

    asyncio.run(request_with_worker(app, send_both))
    assert len(calls) == 5
    crisis_calls = [call for call in calls if call["user_id"] == 10002]
    assert len(crisis_calls) == 1
    assert private not in crisis_calls[0]["message"]
    assert any("我是 AI" in call["message"] for call in calls)
    monkeypatch.delenv("ALERT_QQ")


@pytest.mark.timeout(10)
def test_crisis_rate_limit_and_severity_upgrade(setup_app, monkeypatch):
    app, calls, alerter, _ = setup_app
    monkeypatch.setenv("ALERT_QQ", "10002")
    app.state.processor.alert_qq = "10002"
    app.state.processor.settings.app.setdefault("tools", {}).setdefault(
        "alert_private_msg", {}
    )["enabled"] = True

    async def send(text):
        body = encoded_event(text=text)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/onebot/event", content=body, headers={"X-Signature": signed(body)}
            )

    async def first_two():
        await send("活着没意思")
        await send("活着没意思")
        await drain(app)
        await send("我想自杀，准备好了")
    asyncio.run(request_with_worker(app, first_two))
    assert len(alerter.items) == 2 and len(calls) == 5
    assert alerter.items[-1][1] == "明确自伤计划"


def test_injected_brain_tool_limits_aligned_to_settings(setup_app):
    """E 路修复 1：外部注入 brain 时，其 client.tool_limits 必须被
    settings 口径覆盖（含上限为 0 的情形），不得静默沿用默认。
    无 tool_limits 属性的 client 不被强加该属性。
    """
    from dataclasses import replace as _replace

    from companion.brain import CompanionBrain, DeepSeekClient, EchoStub
    from companion.config import validate_app_config
    from companion.ingress import EventProcessor
    from companion.tool_loop import ToolLoopLimits

    _, _, _, settings = setup_app
    config = validate_app_config({
        "allowed_private_user_ids": ["10001"],
        "llm": {"tool_loop": {"max_tool_rounds": 0, "max_tool_calls": 7}},
    })
    configured = _replace(settings, config=config)

    class _Sender:
        async def send_private_msg(self, user_id, message, *, priority="normal"):
            return {"status": "ok"}

        async def send_alert_private_msg(self, user_id, message):
            return {"status": "ok"}

    class _Alerter:
        async def alert(self, user_id, reason, summary="sensitive text omitted"):
            return None

    # 注入带默认上限（2/3）的 client + settings 自定义上限（0/7）。
    brain = CompanionBrain(configured.persona, DeepSeekClient())
    assert brain.client.tool_limits == ToolLoopLimits()
    EventProcessor(configured, brain=brain, sender=_Sender(), alerter=_Alerter())
    assert brain.client.tool_limits == ToolLoopLimits(
        max_tool_rounds=0, max_tool_calls=7
    )

    # 无 tool_limits 属性的 client：构造不报错、属性不被强加。
    stub_brain = CompanionBrain(configured.persona, EchoStub())
    EventProcessor(configured, brain=stub_brain, sender=_Sender(), alerter=_Alerter())
    assert not hasattr(stub_brain.client, "tool_limits")


def test_disabled_crisis_copy_does_not_claim_notification():
    from pathlib import Path

    import yaml

    safety = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config/safety.yaml").read_text()
    )
    copy = crisis_reply(["12356"], False, safety)
    assert "我是 AI，没办法替你联系别人" in copy
    assert "通知了真人" not in copy
    assert "通知了真人" in crisis_reply(["12356"], True, safety)


def test_memory_limiter_and_llm_providers(tmp_path, monkeypatch):
    memory = ConversationStore(Database(tmp_path / "memory.db"))
    for i in range(25):
        asyncio.run(memory.add_turn("10001", f"u{i}", f"a{i}"))
    assert len(asyncio.run(memory.get_history("10001"))) == 40
    asyncio.run(memory.forget("10001"))
    assert not asyncio.run(memory.get_history("10001"))
    limiter = RateLimiter(2)
    assert limiter.allow("10001", 0) and limiter.allow("10001", 1)
    assert not limiter.allow("10001", 2)
    assert limiter.allow("10001", 60)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert asyncio.run(DeepSeekClient().generate("hello")) == FALLBACK_REPLY
    assert asyncio.run(OpenAICompatibleClient().generate("hello")) == FALLBACK_REPLY
    assert create_llm_client("deepseek").__class__.__name__ == "DeepSeekClient"
    assert create_llm_client("openai_compatible").__class__.__name__ == "OpenAICompatibleClient"
    assert create_llm_client("codex").__class__ is CodexClient


def test_email_alerter_local_first_and_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    alert = EmailAlerter(JsonlAlerter(tmp_path / "alerts.jsonl"))
    asyncio.run(alert.alert("10001", "低落", "脱敏摘要"))
    record = json.loads((tmp_path / "alerts.jsonl").read_text())
    assert record["category"] == "低落" and "脱敏摘要" in record["summary"]
    assert "raw" not in json.dumps(record)


def test_local_alert_write_failure_raises(tmp_path):
    alert = JsonlAlerter(tmp_path / "missing" / "alerts.jsonl")
    alert.path.parent.rmdir() if alert.path.parent.exists() else None
    alert = JsonlAlerter(tmp_path / "alerts.jsonl")
    alert.path.unlink(missing_ok=True)
    alert.path = tmp_path / "block" / "alerts.jsonl"
    alert.path.parent.write_text("block")
    with pytest.raises(OSError):
        asyncio.run(alert.alert("10001", "低落"))



def test_e2b_judge_retries_then_suspected_crisis():
    calls = {"n": 0}

    class Flaky:
        async def generate(self, _prompt):
            calls["n"] += 1
            raise TimeoutError("judge down")

    gateway = SafetyGateway([], ModelLLMJudge(Flaky()))
    assert asyncio.run(gateway.detect("今天心情一般")) == (True, "疑似危机")
    assert calls["n"] == 2


def test_e2b_judge_fallback_reply_retries_then_suspected_crisis():
    calls = {"n": 0}

    class AlwaysFallback:
        async def generate(self, _prompt):
            calls["n"] += 1
            return FALLBACK_REPLY

    gateway = SafetyGateway([], ModelLLMJudge(AlwaysFallback()))
    assert asyncio.run(gateway.detect("隐晦表达")) == (True, "疑似危机")
    assert calls["n"] == 2


def test_e2b_judge_recovers_on_retry():
    calls = {"n": 0}

    class Recover:
        async def generate(self, _prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("once")
            return "NO"

    gateway = SafetyGateway([], ModelLLMJudge(Recover()))
    assert asyncio.run(gateway.detect("普通闲聊")) == (False, "")
    assert calls["n"] == 2


def test_llm_judge_fallback(monkeypatch):
    async def failed(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr("companion.brain.shutil.which", lambda _: "fake")
    monkeypatch.setattr("companion.brain.asyncio.create_subprocess_exec", failed)
    assert asyncio.run(CodexClient().generate("hello")) == FALLBACK_REPLY

    class Yes:
        async def generate(self, _prompt):
            return "YES"

    assert asyncio.run(SafetyGateway([], ModelLLMJudge(Yes())).detect("ambiguous")) == (
        True,
        "model",
    )


def test_tools_throw_when_disabled_and_friend_when_enabled_is_pending():
    client = HttpOneBotClient()

    async def run():
        with pytest.raises(PermissionError):
            await client.qzone_post("test", dry_run=False)
        with pytest.raises(PermissionError):
            await client.manage_friend("10002", "add", dry_run=False)
        with pytest.raises(PermissionError):
            await client.manage_friend_when_enabled("10002", "add", enabled=True)
        configured = HttpOneBotClient(policies={"friend_management": {"enabled": True}})
        assert await configured.manage_friend_when_enabled("10002", "add", enabled=True) == {
            "status": "pending"
        }

    asyncio.run(run())


def test_alerter_failure_still_sends_crisis_reply(setup_app):
    app, calls, _, _ = setup_app

    class BrokenAlerter:
        async def alert(self, *_args, **_kwargs):
            raise OSError("disk unavailable")

    app.state.processor.alerter = BrokenAlerter()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/onebot/event",
                content=encoded_event(text="想自杀"),
                headers={"X-Signature": signed(encoded_event(text="想自杀"))},
            )

    asyncio.run(request_with_worker(app, run))
    assert calls[-1]["message"] == "我是 AI，没办法替你联系别人。"


def test_webhook_returns_without_llm_and_deduplicates_message_id(setup_app):
    app, calls, _, _ = setup_app
    event = private_event(text="普通消息")
    event["message_id"] = "slow-1"
    body = json.dumps(event, ensure_ascii=False).encode()

    class SlowJudge:
        async def generate(self, _prompt):
            await asyncio.sleep(0.15)
            return "NO"

    app.state.processor.safety.judge.client = SlowJudge()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = time.monotonic()
            first = await client.post(
                "/onebot/event", content=body, headers={"X-Signature": signed(body)}
            )
            elapsed = time.monotonic() - started
            second = await client.post(
                "/onebot/event", content=body, headers={"X-Signature": signed(body)}
            )
            await drain(app)
            return first, second, elapsed

    first, second, elapsed = asyncio.run(request_with_worker(app, run))
    assert first.status_code == second.status_code == 204
    assert elapsed < 0.1
    assert sum(item["message"] == "测试回复" for item in calls) == 1


def test_onboarding_once_and_forget_bypasses_llm(setup_app):
    app, calls, _, settings = setup_app
    seen_prompts = []

    class TrackingLLM:
        async def generate(self, prompt):
            seen_prompts.append(prompt)
            return "生成回复"

    from companion.brain import CompanionBrain

    app.state.processor.brain = CompanionBrain(settings.persona, TrackingLLM())

    async def send_same_user_twice():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for text in ("你好", "忘掉我"):
                body = encoded_event(text=text)
                await client.post(
                    "/onebot/event", content=body, headers={"X-Signature": signed(body)}
                )
        await drain(app)

    asyncio.run(request_with_worker(app, send_same_user_twice))
    onboarding = [
        item for item in calls if "阿年" in item["message"] and "忘掉我" in item["message"]
    ]
    assert len(onboarding) == 1
    assert onboarding[0]["message"] == (
        "你好呀，我是阿年，一个 AI 陪伴助手。"
        "我能记住咱们最近聊过的事；如果你不想留着，直接跟我说「忘掉这件事」或「忘掉我」就行。"
        "想聊什么都可以，我在。"
    )
    assert "第三方模型" not in onboarding[0]["message"]
    assert len(seen_prompts) == 1
    assert any("已删除" in item["message"] for item in calls)
    assert not asyncio.run(app.state.processor.conversation.get_history("10001"))


def test_codex_provider_default_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEBOT_SECRET", SECRET)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "codex.yaml"))
    (tmp_path / "codex.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"], "llm": {"provider": "codex"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALLOW_INSECURE_CODEX", raising=False)
    with pytest.raises(RuntimeError, match="ALLOW_INSECURE_CODEX=1"):
        load_settings()


def test_tool_gateway_enforces_configuration_and_per_user_rate_limit():
    from companion.tools import HttpOneBotClient, ToolGateway

    gateway = ToolGateway({"send_private_msg": {"enabled": True}, "qzone": {"enabled": False}})
    client = HttpOneBotClient(gateway=gateway)

    async def run():
        first = await client.send_private_msg("u1", "one", dry_run=True, rate_limit=1)
        second = await client.send_private_msg("u1", "two", dry_run=True, rate_limit=1)
        other = await client.send_private_msg("u2", "three", dry_run=True, rate_limit=1)
        with pytest.raises(PermissionError):
            await client.qzone_post("post", dry_run=False)
        return first, second, other

    first, second, other = asyncio.run(run())
    assert first["status"] == "dry_run"
    assert second["status"] == "rate_limited"
    assert other["status"] == "dry_run"


def test_http_client_rejects_boundary_overrides_with_external_gateway():
    """复审 P4 补钉（第 3 轮）：外部 ActionGateway 自带构造期口径
    （L3 解锁名单、资源根/基准目录）——客户端再传覆写参数必须
    ValueError，防事后改写被静默吞掉、网关边界与装配意图分叉；
    不带覆写时外部网关照常接纳。"""
    from companion.adapters.fake import FakeAdapter
    from companion.tools import HttpOneBotClient

    gateway = FakeAdapter().action_gateway
    with pytest.raises(ValueError, match="l3_unlocked"):
        HttpOneBotClient(
            gateway=gateway, l3_unlocked=frozenset({"send_private_msg"})
        )
    with pytest.raises(ValueError, match="resource_roots"):
        HttpOneBotClient(gateway=gateway, resource_roots=("/tmp/roots",))
    with pytest.raises(ValueError, match="resource_roots"):
        HttpOneBotClient(gateway=gateway, resource_base_dir="/tmp/roots")
    client = HttpOneBotClient(gateway=gateway)
    assert client.action_gateway is gateway


@pytest.mark.parametrize("message,expected", [
    ("想自杀", "我是 AI，没办法替你联系别人。"),
    ("忘掉我", "好的，我已删除保存的短期记忆。"),
])
def test_critical_replies_bypass_full_gateway_limit(setup_app, monkeypatch, message, expected):
    app, _, _, _ = setup_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    sender = HttpOneBotClient(policies={"send_private_msg": {"enabled": True, "rate_limit": 1}})
    processor.sender = sender

    async def run():
        assert (await sender.send_private_msg("10001", "普通消息"))["status"] == "ok"
        assert (await sender.send_private_msg("10001", "超限"))["status"] == "rate_limited"
        return await processor.handle(private_event(text=message))

    result = asyncio.run(run())
    assert any(expected in call["message"] for call in calls[1:])
    assert result


def test_onboarding_uses_the_shared_normal_send_policy(setup_app, monkeypatch):
    app, _, _, _ = setup_app
    processor = app.state.processor
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    sender = HttpOneBotClient(policies={"send_private_msg": {"enabled": True, "rate_limit": 1}})
    processor.sender = sender

    async def run():
        assert (await sender.send_private_msg("10001", "普通消息"))["status"] == "ok"
        return await processor.handle(private_event(text="你好"))

    result = asyncio.run(run())
    assert not result
    assert not asyncio.run(processor.conversation.is_onboarded("10001"))
    assert len(calls) == 1  # the normal bucket is shared, so onboarding did not send


@pytest.mark.parametrize("status", ["rate_limited", "disabled", "error", "exception"])
@pytest.mark.parametrize("message,kind", [
    ("想自杀", "危机话术"),
    ("忘掉我", "忘掉确认"),
    ("你好", "首次告知"),
])
def test_reply_failure_is_logged(setup_app, caplog, status, message, kind):
    app, _, _, _ = setup_app
    processor = app.state.processor
    if kind != "首次告知":
        asyncio.run(processor.conversation.mark_onboarded("10001"))

    class FailedSender:
        async def send_private_msg(self, _user_id, _message, *, priority="normal"):
            if status == "exception":
                raise OSError("private message content")
            return {"status": status}

    processor.sender = FailedSender()
    with caplog.at_level("ERROR", logger="companion.events"):
        result = asyncio.run(processor.handle(private_event(text=message)))
    assert not result
    assert f"{kind} 发送失败" in caplog.text
    assert "private message content" not in caplog.text
    if kind == "首次告知":
        assert not asyncio.run(processor.conversation.is_onboarded("10001"))


def test_critical_gateway_respects_disabled_and_dry_run():
    async def run():
        disabled = HttpOneBotClient(policies={"send_private_msg": {"enabled": False}})
        dry = HttpOneBotClient(policies={"send_private_msg": {"enabled": True, "dry_run": True}})
        disabled_result = await disabled.send_private_msg("10001", "text", priority="critical")
        dry_result = await dry.send_private_msg("10001", "text", priority="critical")
        assert disabled_result["status"] == "disabled"
        assert dry_result["status"] == "dry_run"

    asyncio.run(run())

def test_disabled_default_and_lifespan_shutdown(setup_app, monkeypatch):
    from companion.ingress import create_app

    base_app, *_ = setup_app
    settings = base_app.state.processor.settings
    events = []

    class FakeMonitor:
        def __init__(self, _url):
            events.append("created")

        async def run(self):
            events.append("started")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("stopped")

    monkeypatch.setattr("companion.ingress.ConnectionHealth", FakeMonitor)

    async def scenario():
        async with base_app.router.lifespan_context(base_app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=base_app),
                                         base_url="http://test") as client:
                assert (await client.get("/health/ready")).status_code == 404
            assert not hasattr(base_app.state, "connection_health")
        assert not events
        from dataclasses import replace
        configured = replace(settings, app={**settings.app, "connection_health": {"enabled": True}})
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert events == ["created", "started"]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/health/ready")).json() == {"ready": True}
        assert events == ["created", "started", "stopped"]
    asyncio.run(scenario())
