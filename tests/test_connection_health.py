import asyncio
import logging

import httpx
import pytest

from companion.adapters.transport import TransportError
from companion.ops.connection_health import (
    CEILING,
    ConnectionHealth,
    UnitState,
    get_json,
    local_url,
    systemd_state,
)
from companion.tools import HttpOneBotClient


class FakeWorld:
    def __init__(self):
        self.now = 0
        self.units = {name: UnitState("active", "success") for name in ("companion", "napcat")}
        self.ingress = ("ok", {"ready": True})
        self.onebot = ("ok", {"status": "ok", "retcode": 0,
                              "data": {"online": True, "good": True}})
        self.requests = []
        self.running = 0
        self.max_running = 0

    async def unit(self, name):
        return self.units["napcat" if "napcat" in name else "companion"]

    async def probe(self, url):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        await asyncio.sleep(0)
        self.requests.append(url.rsplit("/", 1)[-1])
        self.running -= 1
        return self.ingress if url.endswith("/health/ready") else self.onebot

    def monitor(self):
        return ConnectionHealth("http://127.0.0.1:3000", probe=self.probe,
                                unit_state=self.unit, clock=lambda: self.now,
                                jitter=lambda _a, _b: 1)


def test_short_disconnect_recovery_and_stable_reset(caplog):
    async def scenario():
        world = FakeWorld()
        health = world.monitor()
        world.onebot = ("disconnected", None)
        assert await health.tick() == 5
        assert health.snapshot.onebot == "disconnected"
        assert health.snapshot.qq == "unconfirmed"
        assert health.snapshot.action == "operator_action_needed"
        assert await health.tick() == 10
        world.onebot = ("ok", {"status": "ok", "retcode": 0,
                               "data": {"online": True, "good": True}})
        assert await health.tick() == 30
        assert health.snapshot.qq == "reported_online"
        assert health.failures == 2
        world.now = 119
        await health.tick()
        assert health.failures == 2
        world.now = 120
        await health.tick()
        assert health.failures == 0
        world.onebot = ("disconnected", None)
        assert await health.tick() == 5
        assert set(world.requests) == {"ready", "get_status"}
        assert len(world.requests) == 12
    with caplog.at_level(logging.INFO, logger="companion.connection_health"):
        asyncio.run(scenario())
    assert "operator_action_needed" in caplog.text
    assert "reported_online" in caplog.text


def test_prolonged_outage_is_capped_and_never_controls_services():
    async def scenario():
        world = FakeWorld()
        world.onebot = ("disconnected", None)
        health = world.monitor()
        delays = [await health.tick() for _ in range(30)]
        assert delays[:7] == [5, 10, 20, 40, 80, 160, 300]
        assert all(1 <= delay <= CEILING for delay in delays)
        assert len(world.requests) == 60
        assert set(world.requests) == {"ready", "get_status"}
    asyncio.run(scenario())


@pytest.mark.parametrize("unit", [UnitState("inactive", "success"),
                                  UnitState("failed", "start-limit-hit")])
def test_stopped_or_start_limited_service_is_not_probed(unit):
    async def scenario():
        world = FakeWorld()
        world.units["napcat"] = unit
        health = world.monitor()
        assert await health.tick() == CEILING
        assert health.snapshot.napcat_result == unit.result
        assert health.snapshot.onebot == "not_probed"
        assert health.snapshot.action == "operator_action_needed"
        assert world.requests == ["ready"]
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    {"status": "ok", "retcode": 0, "data": {"online": False, "good": True}},
    {"status": "ok", "retcode": 0, "data": {"online": None, "good": True}},
    {"status": "failed", "retcode": 1, "data": {}},
])
def test_expired_or_unknown_login_requires_operator_and_no_escalation(payload):
    async def scenario():
        world = FakeWorld()
        world.onebot = ("ok", payload)
        health = world.monitor()
        for _ in range(4):
            assert await health.tick() == CEILING
        assert health.snapshot.action == "operator_action_needed"
        assert health.snapshot.qq in {"offline", "unconfirmed"}
        assert health.failures == 0
        assert set(world.requests) == {"ready", "get_status"}
    asyncio.run(scenario())


def test_concurrent_faults_are_serialized_and_shutdown_cancels():
    async def scenario():
        world = FakeWorld()
        world.onebot = ("disconnected", None)
        world.ingress = ("disconnected", None)
        health = world.monitor()
        assert await asyncio.gather(health.tick(), health.tick()) == [5, 10]
        assert world.max_running == 1
        assert health.snapshot.ingress == "unready"
        started = asyncio.Event()
        async def wait(_delay):
            started.set()
            await asyncio.Event().wait()
        health.sleep = wait
        task = asyncio.create_task(health.run())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(world.requests) == 6
    asyncio.run(scenario())


def test_timeout_and_fresh_clients_only_safe_probe(monkeypatch):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append((request.method, request.url.path))
            raise httpx.ReadTimeout("private details", request=request)
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        created = []
        def client(**kwargs):
            assert kwargs["trust_env"] is False
            created.append(1)
            return original(transport=transport, **kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", client)
        assert await get_json("http://127.0.0.1:3000/get_status") == ("disconnected", None)
        assert await get_json("http://127.0.0.1:3000/get_status") == ("disconnected", None)
        assert calls == [("GET", "/get_status")] * 2
        assert len(created) == 2
    asyncio.run(scenario())


def test_temporary_systemd_like_supervisor_is_only_read(monkeypatch, tmp_path):
    script = tmp_path / "systemctl"
    log = tmp_path / "calls"
    script.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + str(log) + '"\n'
                      'printf "ActiveState=failed\\nResult=start-limit-hit\\n"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    state = asyncio.run(systemd_state("qqbot-napcat.service"))
    assert state == UnitState("failed", "start-limit-hit")
    assert log.read_text().strip() == (
        "show qqbot-napcat.service --property=ActiveState --property=Result"
    )


def test_ambiguous_send_is_not_retried_after_health_recovery(monkeypatch):
    async def scenario():
        requests = []
        async def handler(request):
            requests.append((request.method, request.url.path))
            raise httpx.ReadTimeout("ambiguous", request=request)
        original = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
        client = HttpOneBotClient()
        for priority in ("normal", "critical"):
            # N1 传输收敛：超时仍向上冒泡且不重试，但异常类型已收敛为
            # 传输层自己的 TransportError（httpx 类型不再穿透客户端）。
            with pytest.raises(TransportError):
                await client.send_private_msg("10001", "synthetic", priority=priority)
        world = FakeWorld()
        await world.monitor().tick()
        assert requests == [("POST", "/send_private_msg")] * 2
        assert world.requests == ["ready", "get_status"]
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["https://127.0.0.1:3000", "http://example.com:3000",
                                 "http://127.0.0.1:3000/path", "http://user@localhost:3000"])
def test_only_fixed_local_origins_are_accepted(url):
    with pytest.raises(ValueError):
        local_url(url)


def test_health_probe_total_timeout_and_size_limit(monkeypatch):
    import companion.ops.connection_health as module

    async def scenario():
        original = httpx.AsyncClient
        async def hanging(_request):
            await asyncio.Event().wait()
        monkeypatch.setattr(module, "PROBE_TIMEOUT", 0.01)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
            transport=httpx.MockTransport(hanging), **kw))
        assert await get_json("http://127.0.0.1:3000/get_status") == ("disconnected", None)
        async def large(_request):
            return httpx.Response(200, content=b"x" * (module.MAX_BODY + 1))
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
            transport=httpx.MockTransport(large), **kw))
        assert await get_json("http://127.0.0.1:3000/get_status") == ("invalid", None)
    asyncio.run(scenario())




def test_jitter_is_bounded_and_transition_logs_are_sanitized(caplog):
    async def scenario():
        world = FakeWorld()
        health = world.monitor()
        health.jitter = lambda _low, high: high
        world.onebot = ("disconnected", {"secret": "synthetic-sensitive-marker"})
        for _ in range(20):
            assert 1 <= await health.tick() <= CEILING
        world.onebot = ("ok", {"status": "failed", "retcode": 1,
                               "data": {"online": False},
                               "message": "synthetic-sensitive-marker"})
        assert await health.tick() == CEILING
        assert health.snapshot.qq == "unconfirmed"
    with caplog.at_level(logging.INFO, logger="companion.connection_health"):
        asyncio.run(scenario())
    assert "synthetic-sensitive-marker" not in caplog.text


def test_shutdown_cancels_inflight_probe():
    async def scenario():
        world = FakeWorld()
        started, stopped = asyncio.Event(), asyncio.Event()
        async def hanging(_url):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        health = world.monitor()
        health.probe = hanging
        task = asyncio.create_task(health.run())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
    asyncio.run(scenario())
