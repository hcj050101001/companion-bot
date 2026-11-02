"""Golden OneBot payload fixtures driven through the current ingress.

Fixtures live in ``tests/fixtures/golden/*.json`` and are fully synthetic
(fake self/user/group ids, fake message ids, no real identifiers or secrets).
Each fixture annotates the disposition required by the framework plan
(``plans/2026-10-31-qqbot-framework-architecture.md`` §3.1/§5.2/§5.3):

- ``accepted``: HTTP 204 and the event is processed (replies are sent);
- ``ignored``: HTTP 204, zero sends, and an ``ignored`` entry in the event log;
- ``sends`` counts every outbound private message, including the one-time
  onboarding notice, after the payload (and its optional follow-up) drains.
- Selected parsing/routing-sensitive fixtures additionally carry
  ``expected_text`` (the normalized user text as stored in short-term
  history; ``""`` for media-only turns, which store no history) and
  ``expected_recipient`` (the user_id every outbound send must target).
  These are only added where normalization is the point of the case
  (segment text, text+image, CQ stripping, numeric ids, media-only).

Where the current implementation diverges from that contract, the fixture
carries ``known_difference: "KD-Cxx"`` and the case runs as
``xfail(strict=True)`` registered in ``tests/test_adapter_contract_p0.py``.
"""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from tests import test_app

setup_app = test_app.setup_app
SECRET = test_app.SECRET
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "golden"


def _signed(body: bytes) -> str:
    return "sha1=" + hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()


def _body(case: dict, payload: dict | None = None) -> bytes:
    if payload is not None:
        return json.dumps(payload, ensure_ascii=False).encode()
    if case.get("raw_body") is not None:
        return str(case["raw_body"]).encode()
    return json.dumps(case["payload"], ensure_ascii=False).encode()


def _load_cases():
    cases = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        marks = []
        known = case.get("known_difference")
        if known:
            marks.append(
                pytest.mark.xfail(
                    strict=True,
                    reason=f"{known}: {case.get('difference_note') or 'registered difference'}",
                )
            )
        cases.append(pytest.param(case, id=case["id"], marks=marks))
    return cases


@pytest.mark.parametrize("case", _load_cases())
@pytest.mark.timeout(10)
def test_golden_payload(setup_app, case):
    app, calls, _, settings = setup_app
    expected = case["expected"]

    async def scenario():
        statuses = []

        async def post(body: bytes):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/onebot/event", content=body, headers={"X-Signature": _signed(body)}
                )
            statuses.append(response.status_code)
            await test_app.drain(app)

        body = _body(case)
        await post(body)
        if case.get("send_twice"):
            await post(body)
        followup = case.get("followup_payload")
        if followup is not None:
            await post(_body(case, payload=followup))
        return statuses

    statuses = asyncio.run(test_app.request_with_worker(app, scenario))
    assert statuses == [expected["http_status"]] * len(statuses), case["id"]
    assert len(calls) == expected["sends"], case["id"]
    needle = expected.get("reply_contains")
    if needle:
        assert any(needle in call["message"] for call in calls), case["id"]
    recipient = expected.get("expected_recipient")
    if recipient is not None:
        # Every outbound send (onboarding included) must target the fixture's
        # recipient — a routing slip to another user must fail the case.
        assert calls, case["id"]
        assert all(call["user_id"] == recipient for call in calls), case["id"]
    expected_text = expected.get("expected_text")
    if expected_text is not None:
        # The normalized text the model pipeline actually consumed, observed
        # via the stored short-term history for the recipient (CQ codes and
        # non-text segments already stripped, segment text already joined).
        history = asyncio.run(app.state.processor.conversation.get_history(str(recipient)))
        if expected_text:
            assert ("user", expected_text) in history, case["id"]
        else:
            # Media-only turns store no history at all.
            assert history == [], case["id"]
    log_event_type = expected.get("log_event_type")
    if log_event_type:
        records = [
            json.loads(line)
            for line in (settings.root / "logs/events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert records and records[-1]["event_type"] == log_event_type, case["id"]
