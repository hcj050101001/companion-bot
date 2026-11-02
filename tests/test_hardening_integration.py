"""Cross-phase regression using only in-process transports and synthetic data."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from companion.brain import CompanionBrain, DeepSeekClient
from companion.memory.care import CarePreferences
from companion.ops.connection_health import ConnectionHealth, UnitState
from companion.safety import crisis_reply
from companion.safety.care import CareSendGate
from companion.tools import HttpOneBotClient
from companion.tools.plugins import PluginRegistry, WeatherPlugin
from companion.tools.read_only import ReadOnlyRegistry
from tests.fake_onebot import make_fake_onebot, private_event
from tests.test_app import SECRET, request_with_worker, signed
from tests.test_read_only_tools import call, reply
from tests.test_weather import adapter

pytest_plugins = ["tests.test_app"]


@pytest.mark.parametrize(
    "global_enabled,weather_enabled", [(False, False), (True, False), (False, True), (True, True)]
)
@pytest.mark.parametrize("user", ["10001", "10002"])
def test_signed_ingress_weather_outage_crisis_health_and_care(
    setup_app, monkeypatch, tmp_path, global_enabled, weather_enabled, user
):
    app, _, _, settings = setup_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded(user))
    fake_onebot = make_fake_onebot(SECRET)
    weather, weather_requests = adapter()
    registry = ReadOnlyRegistry(
        enabled=global_enabled,
        policies={"weather_lookup": {"enabled": weather_enabled, "rate_limit": 10}},
    )
    PluginRegistry(registry).register_plugin(WeatherPlugin(weather))
    processor.read_only_registry = registry
    processor.brain = CompanionBrain("synthetic persona", DeepSeekClient())
    processor.sender = HttpOneBotClient("http://fake-onebot")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    original_client = httpx.AsyncClient
    model_requests = []
    fail_provider = False
    real_get = weather.get

    async def fake_weather(endpoint, params):
        if fail_provider:
            weather_requests.append((endpoint, params))
            raise httpx.ReadTimeout("synthetic provider failure")
        return await real_get(endpoint, params)

    weather.get = fake_weather

    async def route(request):
        if request.url.host == "fake-onebot":
            return await httpx.ASGITransport(app=fake_onebot).handle_async_request(request)
        assert request.url.host == "api.deepseek.com"
        payload = json.loads(request.content)
        model_requests.append(payload)
        if "tools" in payload and "Testville" in payload["messages"][-1]["content"]:
            return httpx.Response(200, json=reply(calls=[call(
                "weather", name="weather_lookup",
                arguments='{"city":"Testville","day":"today"}',
            )]))
        if "tools" in payload:
            return httpx.Response(200, json=reply("synthetic ordinary reply"))
        # The tool-less ordinary path asks for one bounded structured object.
        return httpx.Response(200, json=reply(
            '{"bubbles": ["synthetic ordinary reply"]}'
        ))

    class FakeHTTP(original_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(route)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", FakeHTTP)

    async def scenario():
        nonlocal fail_provider
        async def send(text, number):
            event = private_event(user, text)
            event["message_id"] = str(number)
            body = json.dumps(event, ensure_ascii=False).encode()
            async with original_client(
                transport=httpx.ASGITransport(app=app), base_url="http://app"
            ) as client:
                response = await client.post(
                    "/onebot/event", content=body, headers={"X-Signature": signed(body)}
                )
            assert response.status_code == 204

        async def events():
            nonlocal fail_provider
            await send("Testville weather", 2)
            await app.state.event_bus.join()
            await app.state.processor.wait_idle()
            enabled = global_enabled and weather_enabled
            assert len(weather_requests) == (2 if enabled else 0)
            if enabled:
                assert "Open-Meteo" in fake_onebot.state.calls[-1]["message"]
                fail_provider = True
                await send("Testville weather", 3)
                await app.state.event_bus.join()
                await app.state.processor.wait_idle()
                assert "不可用" in fake_onebot.state.calls[-1]["message"]
            await send("ordinary synthetic", 4)
            await app.state.event_bus.join()
            await app.state.processor.wait_idle()
            assert fake_onebot.state.calls[-1]["message"] == "synthetic ordinary reply"
            before = (len(model_requests), len(weather_requests))
            await send("想自杀", 5)
            await app.state.event_bus.join()
            await app.state.processor.wait_idle()
            assert fake_onebot.state.calls[-1]["message"] == crisis_reply(
                settings.safety["helplines"], False, settings.safety
            )
            assert (len(model_requests), len(weather_requests)) == before

            # A health outage/recovery performs probes only and cannot replay sends.
            health_status = [False]
            probes = []

            async def probe(url):
                probes.append(url.rsplit("/", 1)[-1])
                if url.endswith("/health/ready"):
                    return "ok", {"ready": True}
                if not health_status[0]:
                    return "disconnected", None
                return "ok", {"status": "ok", "retcode": 0,
                              "data": {"online": True, "good": True}}

            async def unit(_name):
                return UnitState("active", "success")

            monitor = ConnectionHealth("http://127.0.0.1:3000", probe=probe, unit_state=unit)
            sent = len(fake_onebot.state.calls)
            await monitor.tick()
            assert monitor.snapshot.action == "operator_action_needed"
            health_status[0] = True
            await monitor.tick()
            assert monitor.snapshot.action == "none"
            assert probes == ["ready", "get_status"] * 2

            # Even an opted-in preference cannot send with the default-off gate.
            now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
            store = CarePreferences(tmp_path / "private" / "care.db")
            store.opt_in("10001", now=now, expires=now + timedelta(days=1))
            gate = CareSendGate(store, processor.sender, whitelist=lambda _: True,
                                current_safety=lambda _: (processor.safety, True, 0),
                                clock=lambda: now)
            assert not await gate.send("10001", "synthetic-work", now)
            assert len(fake_onebot.state.calls) == sent

        await request_with_worker(app, events)

    asyncio.run(scenario())
    log = processor.log_path.read_text()
    assert "Testville" not in log and "ordinary synthetic" not in log and "10001" not in log
