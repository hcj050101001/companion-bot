"""Synthetic tests for shared send accounting and bounded outbound concurrency."""

import asyncio

import httpx

from companion.quota import QuotaUnavailable, SendQuota
from companion.tools import HttpOneBotClient


def test_per_user_and_account_normal_windows_roll():
    now = [0.0]
    quota = SendQuota(
        clock=lambda: now[0], normal_per_user=3, account_normal=5, window_seconds=60
    )
    assert quota.reserve("u1", 3) is not None
    assert quota.reserve("u1", 1) is None
    assert quota.reserve("u2", 2) is not None
    assert quota.reserve("u3", 1) is None
    now[0] = 61
    assert quota.reserve("u3", 1) is not None


def test_critical_budget_is_separate_and_bounded():
    quota = SendQuota(account_normal=1, account_critical=2)
    assert quota.reserve("u", 1, critical=True) is not None
    assert quota.reserve("u", 1, critical=True) is not None
    assert quota.reserve("u", 1, critical=True) is None
    assert quota.reserve("u", 1) is not None


def test_partial_multipart_grants_exactly_one_or_all():
    partial = SendQuota(normal_per_user=2).reserve("u", 3, allow_partial_single=True)
    assert partial is not None and partial.remaining == 1
    assert SendQuota(normal_per_user=0).reserve(
        "u", 3, allow_partial_single=True
    ) is None
    full = SendQuota(normal_per_user=3).reserve("u", 3, allow_partial_single=True)
    assert full is not None and full.remaining == 3


def test_reserved_tokens_are_single_use_and_bound_to_their_quota():
    quota = SendQuota()
    reservation = quota.reserve("u", 2)
    token = reservation.consume()
    assert quota.accept_token(token, critical=False)
    assert not quota.accept_token(token, critical=False)
    assert not quota.accept_token(token, critical=True)
    foreign = reservation.consume()
    assert not SendQuota().accept_token(foreign, critical=False)


def test_reserved_tokens_are_bound_to_the_reserved_user():
    quota = SendQuota()
    token = quota.reserve("10001", 1).consume()
    assert not quota.accept_token(token, critical=False, identity="10002")
    assert quota.accept_token(token, critical=False, identity="10001")


def test_gateway_uses_pre_reserved_tokens_without_double_charge(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "ok"})

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    quota = SendQuota(normal_per_user=4)
    client = HttpOneBotClient(
        policies={"send_private_msg": {"enabled": True, "rate_limit": 20}}, quota=quota
    )
    reservation = quota.reserve("10001", 2)

    async def run():
        first = await client.send_private_msg(
            "10001", "one", quota_token=reservation.consume()
        )
        second = await client.send_private_msg(
            "10001", "two", quota_token=reservation.consume()
        )
        # Only the two reserved attempts were charged; two more fit, a third does not.
        assert quota.reserve("10001", 2) is not None
        assert quota.reserve("10001", 1) is None
        return first, second

    first, second = asyncio.run(run())
    assert first == second == {"status": "ok"}
    assert len(calls) == 2


def test_gateway_fails_closed_when_accounting_is_unavailable(monkeypatch):
    quota = SendQuota()

    def unavailable(*_args, **_kwargs):
        raise QuotaUnavailable("synthetic")

    monkeypatch.setattr(quota, "reserve", unavailable)
    original = httpx.AsyncClient

    class MustNotConstruct(original):
        def __init__(self, *args, **kwargs):
            raise AssertionError("outbound HTTP must not run when accounting fails")

    monkeypatch.setattr(httpx, "AsyncClient", MustNotConstruct)
    client = HttpOneBotClient(
        policies={"send_private_msg": {"enabled": True, "rate_limit": 20}}, quota=quota
    )
    assert asyncio.run(client.send_private_msg("10001", "text")) == {
        "status": "unavailable"
    }


def test_gateway_rejects_foreign_and_reused_tokens(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "ok"})

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    quota = SendQuota()
    client = HttpOneBotClient(
        policies={"send_private_msg": {"enabled": True, "rate_limit": 20}}, quota=quota
    )
    foreign = SendQuota().reserve("10001", 1).consume()
    assert asyncio.run(client.send_private_msg("10001", "text", quota_token=foreign)) == {
        "status": "unavailable"
    }
    token = quota.reserve("10001", 1).consume()
    assert asyncio.run(client.send_private_msg("10001", "text", quota_token=token)) == {
        "status": "ok"
    }
    assert asyncio.run(client.send_private_msg("10001", "text", quota_token=token)) == {
        "status": "unavailable"
    }
    assert len(calls) == 1


def test_concurrency_reserves_the_second_slot_for_critical():
    async def run():
        quota = SendQuota(slot_wait_seconds=0.05)
        assert await quota.acquire_slot(False)
        assert await quota.acquire_slot(True)
        assert not await quota.acquire_slot(True)
        assert not await quota.acquire_slot(False)
        quota.release_slot(True)
        assert await quota.acquire_slot(True)
        quota.release_slot(True)
        quota.release_slot(False)

    asyncio.run(run())
