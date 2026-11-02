"""P2 adapter contract unit tests (plan §5.2, KD-C01~C07 closure).

Pure unit level: ``normalize_onebot_event`` / ``OneBotAdapter.normalize``
turn raw OneBot payloads into ``InboundEvent`` or ``Ignore`` carrying a
``reason_code``; ``FakeAdapter`` is the in-memory test double recording
sends with preset receipts/health/delay; ``OneBotAdapter.send_private``
maps the wrapped client result onto the ``SendReceipt`` three-state
outcome (accepted / rejected / ambiguous). Everything here uses fake
objects only — no network, no ingress, no real QQ.
"""

import asyncio
import inspect
import time

import pytest

from companion import adapters as adapters_mod
from companion.adapters import (
    AdapterHealth,
    DeliveryHandle,
    FakeAdapter,
    Ignore,
    InboundEvent,
    OneBotAdapter,
    SendReceipt,
    normalize_onebot_event,
)

pytestmark = pytest.mark.timeout(10)

_TEXT_SEGMENT_TYPES = {"TextSegment", "Text"}


# ---------------------------------------------------------------------------
# Helpers (signature-adaptive only where the plan leaves the shape open)
# ---------------------------------------------------------------------------


def base_event(**overrides):
    event = {
        "time": 1767225600,
        "self_id": "900000001",
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": "p2-1",
        "user_id": "90001",
        "message": "你好，今天有点累",
        "raw_message": "你好，今天有点累",
        "sender": {"user_id": 90001, "nickname": "fake-user"},
        "font": 0,
    }
    event.update(overrides)
    return event


def reason_of(result) -> str:
    assert isinstance(result, Ignore), f"expected Ignore, got {result!r}"
    return str(getattr(result.reason_code, "value", result.reason_code))


def outcome_of(receipt) -> str:
    assert isinstance(receipt, SendReceipt), f"expected SendReceipt, got {receipt!r}"
    return str(getattr(receipt.outcome, "value", receipt.outcome))


def is_text_segment(segment) -> bool:
    if isinstance(segment, dict):
        return segment.get("type") == "text"
    return (
        type(segment).__name__ in _TEXT_SEGMENT_TYPES
        or getattr(segment, "type", None) == "text"
    )


def segment_text(segment) -> str:
    if isinstance(segment, dict):
        return str(segment.get("data", {}).get("text", ""))
    return str(getattr(segment, "text", ""))


def make_text_segment(text: str):
    for name in ("TextSegment", "Text"):
        cls = getattr(adapters_mod, name, None)
        if cls is None:
            continue
        for args, kwargs in (
            ((), {"text": text}),
            ((text,), {}),
            ((), {"type": "text", "data": {"text": text}}),
        ):
            try:
                return cls(*args, **kwargs)
            except Exception:
                continue
    return {"type": "text", "data": {"text": text}}


def make_delivery_handle():
    """Build a DeliveryHandle, filling required fields by parameter name."""
    if DeliveryHandle is None:
        return None
    params = [
        p
        for p in inspect.signature(DeliveryHandle).parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]

    def fill(token_value):
        kwargs = {}
        for p in params:
            if p.default is not inspect.Parameter.empty:
                continue
            if "priority" in p.name:
                kwargs[p.name] = "normal"
            elif "token" in p.name:
                kwargs[p.name] = token_value
            elif "session" in p.name:
                kwargs[p.name] = "onebot:90001"
            elif "user" in p.name:
                kwargs[p.name] = "90001"
            else:
                kwargs[p.name] = None
        return kwargs

    for token_value in (None, "p2-test-token"):
        try:
            return DeliveryHandle(**fill(token_value))
        except Exception:
            continue
    try:
        return DeliveryHandle()
    except Exception:
        return None


def run_send(adapter, *, text="你好"):
    """Call ``adapter.send_private`` in whatever shape its signature takes."""

    async def _call():
        handle = make_delivery_handle()
        segments = [make_text_segment(text)]
        kwargs = {}
        try:
            params = inspect.signature(adapter.send_private).parameters
        except (TypeError, ValueError):
            params = {}
        named = set(params)
        if named & {"session_id", "user_id", "recipient", "target", "peer_id"}:
            key = next(iter(named & {"session_id", "user_id", "recipient", "target", "peer_id"}))
            kwargs[key] = "onebot:90001" if key == "session_id" else "90001"
            if "session_id" in named and "user_id" in named:
                kwargs["user_id"] = "90001"
        if "segments" in named:
            kwargs["segments"] = segments
        for text_key in ("text", "message", "content"):
            if text_key in named:
                kwargs[text_key] = text
                break
        for handle_key in ("delivery", "handle", "delivery_handle"):
            if handle_key in named and handle is not None:
                kwargs[handle_key] = handle
        if not kwargs:  # generic *args/**kwargs signature: plan §5.2 order
            result = adapter.send_private("onebot:90001", segments, delivery=handle)
        else:
            result = adapter.send_private(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    return asyncio.run(_call())


def make_fake(outcome=None, health=None, delay_seconds=None):
    params = set(inspect.signature(FakeAdapter).parameters)
    kwargs = {}
    if outcome is not None:
        for name in ("outcome", "send_outcome", "receipt_outcome", "default_outcome"):
            if name in params:
                kwargs[name] = outcome
                break
        else:
            for name in ("receipt", "send_receipt", "default_receipt"):
                if name in params:
                    kwargs[name] = SendReceipt(
                        outcome=outcome, platform_message_id=None
                    )
                    break
    if health is not None:
        for name in ("health", "health_value", "adapter_health", "health_result"):
            if name in params:
                kwargs[name] = health
                break
    if delay_seconds is not None and "delay_seconds" in params:
        kwargs["delay_seconds"] = delay_seconds
    fake = FakeAdapter(**kwargs)
    # Attribute-style preset fallback if a knob was not a constructor param.
    if outcome is not None and not (
        params & {"outcome", "send_outcome", "receipt_outcome", "default_outcome",
                  "receipt", "send_receipt", "default_receipt"}
    ):
        for name in ("outcome", "send_outcome", "receipt_outcome", "default_outcome"):
            try:
                setattr(fake, name, outcome)
            except Exception:
                pass
    if health is not None and not (
        params & {"health", "health_value", "adapter_health", "health_result"}
    ):
        for name in ("health_value", "health_result", "adapter_health", "_health"):
            try:
                setattr(fake, name, health)
            except Exception:
                pass
    if delay_seconds is not None and "delay_seconds" not in params:
        try:
            fake.delay_seconds = delay_seconds
        except Exception:
            pass
    return fake


def fake_sends(fake) -> list:
    for name in ("sends", "sent", "calls", "sent_messages", "outbox", "messages"):
        records = getattr(fake, name, None)
        if isinstance(records, list):
            return records
    raise AssertionError(f"FakeAdapter exposes no send record list: {fake!r}")


def call_health(fake):
    async def _call():
        result = fake.health()
        if inspect.isawaitable(result):
            result = await result
        return result

    return asyncio.run(_call())


def make_health(online=True, good=False):
    try:
        return AdapterHealth(online=online, good=good)
    except Exception:
        return AdapterHealth(online, good)


class FakeOneBotClient:
    """Dict-status client with the existing OneBotClient surface."""

    def __init__(self, result=None, error=None):
        self.result = {"status": "ok", "data": {"message_id": 4242}} if result is None else result
        self.error = error
        self.calls = []

    async def _respond(self, record):
        self.calls.append(record)
        if self.error is not None:
            raise self.error
        return self.result

    async def send_private_msg(self, user_id, message, **kwargs):
        return await self._respond({"user_id": user_id, "message": message})

    async def send_private(self, *args, **kwargs):
        return await self._respond({"args": args, "kwargs": kwargs})

    async def call_api(self, action, **kwargs):
        return await self._respond({"action": action, "kwargs": kwargs})

    async def get_status(self):
        return {"online": True, "good": True}


def make_onebot(client) -> OneBotAdapter:
    try:
        return OneBotAdapter(client)
    except TypeError:
        params = list(inspect.signature(OneBotAdapter).parameters)
        return OneBotAdapter(**{params[0]: client}) if params else OneBotAdapter()


# ---------------------------------------------------------------------------
# Exports & reason-code constants
# ---------------------------------------------------------------------------


def test_reason_code_constants_have_contract_values():
    assert adapters_mod.REASON_NOT_PRIVATE == "not_private"
    assert adapters_mod.REASON_NO_MESSAGE_ID == "no_message_id"
    assert adapters_mod.REASON_NO_SELF_ID == "no_self_id"
    assert adapters_mod.REASON_MALFORMED == "malformed"
    assert adapters_mod.REASON_UNSUPPORTED_TYPE == "unsupported_type"


def test_send_receipt_and_delivery_handle_shapes():
    receipt = SendReceipt(outcome="accepted", platform_message_id="m-1")
    assert outcome_of(receipt) == "accepted"
    assert receipt.platform_message_id == "m-1"
    handle = make_delivery_handle()
    assert handle is not None and isinstance(handle, DeliveryHandle)


# ---------------------------------------------------------------------------
# normalize: qualified private events
# ---------------------------------------------------------------------------


def test_normalize_plain_private_text_event_fields():
    result = normalize_onebot_event(base_event())
    assert isinstance(result, InboundEvent)
    assert result.platform == "onebot"
    assert result.session_id == "onebot:90001"
    assert str(result.user_id) == "90001"
    assert str(result.self_id) == "900000001"
    assert str(result.message_id) == "p2-1"
    assert result.text == "你好，今天有点累"
    assert result.has_media is False
    text_segments = [s for s in result.segments if is_text_segment(s)]
    assert text_segments and segment_text(text_segments[0]) == "你好，今天有点累"


def test_normalize_cq_string_strips_codes_from_text():
    event = base_event(
        message="你好[CQ:at,qq=90002]在吗", raw_message="你好[CQ:at,qq=90002]在吗"
    )
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.text == "你好在吗"
    assert "[CQ:" not in result.text


def test_normalize_segment_list_with_image_sets_has_media():
    event = base_event(
        message=[
            {"type": "text", "data": {"text": "看这张图"}},
            {"type": "image", "data": {"file": "fake.jpg",
                                       "url": "http://example.invalid/fake.jpg"}},
        ],
        raw_message="看这张图[CQ:image,file=fake.jpg]",
    )
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.text == "看这张图"
    assert result.has_media is True
    text_segments = [s for s in result.segments if is_text_segment(s)]
    assert text_segments and segment_text(text_segments[0]) == "看这张图"


# ---------------------------------------------------------------------------
# 图片引用契约（2026-10-31 接缝修复）：images 是 normalize 的契约
# 产出，数组形与 CQ 字符串形同口径填充，杜绝消费方替身自带字段
# 与真实事件分叉（真机不触发缺陷的回归钉子）。
# ---------------------------------------------------------------------------

from companion.adapters.base import ImageRef  # noqa: E402
from companion.adapters.onebot import classify_onebot_event  # noqa: E402
from companion.perception import parse_event  # noqa: E402

_IMAGE_URL = "https://images.example.test/cat0.png"
_FAITHFUL_IMAGE_DATA = {
    # 与真机 NapCat 段数组实测键形态一致（值全为假）：summary/
    # sub_type/file_size（字符串）俱在，不得影响引用提取。
    "summary": "",
    "file": "cat0.png",
    "sub_type": 0,
    "url": _IMAGE_URL,
    "file_size": "12345",
}


def test_inbound_event_images_field_contract():
    field_names = [f.name for f in __import__("dataclasses").fields(InboundEvent)]
    assert field_names[-1] == "images"  # 只许末尾追加，旧构造点零改动
    event = base_event()
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.images == ()  # 纯文本默认空元组


def test_normalize_segment_list_fills_images():
    event = base_event(
        message=[{"type": "image", "data": dict(_FAITHFUL_IMAGE_DATA)}],
        raw_message="[CQ:image,file=cat0.png]",
    )
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    # file_size 为字符串报告：size_hint 不采（与既有口径一致）
    assert result.images == (ImageRef(_IMAGE_URL, None, "cat0.png"),)


def test_normalize_segment_list_mixed_fills_images():
    event = base_event(
        message=[
            {"type": "text", "data": {"text": "这是什么"}},
            {"type": "image", "data": dict(_FAITHFUL_IMAGE_DATA)},
        ],
        raw_message="这是什么[CQ:image,file=cat0.png]",
    )
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.text == "这是什么"
    assert result.images == (ImageRef(_IMAGE_URL, None, "cat0.png"),)


def test_normalize_cq_string_fills_images():
    cq = f"[CQ:image,file=cat0.png,url={_IMAGE_URL}]"
    event = base_event(message=cq, raw_message=cq)
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.has_media is True
    assert result.text == ""
    assert result.images == (ImageRef(_IMAGE_URL, None, "cat0.png"),)


def test_normalize_cq_string_mixed_fills_images_and_text():
    cq = f"这是什么[CQ:image,file=cat0.png,url={_IMAGE_URL}]"
    event = base_event(message=cq, raw_message=cq)
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.text == "这是什么"
    assert result.images == (ImageRef(_IMAGE_URL, None, "cat0.png"),)


def test_normalize_cq_string_file_only_ref_kept():
    cq = "[CQ:image,file=cat0.png]"
    event = base_event(message=cq, raw_message=cq)
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.images == (ImageRef("cat0.png", None, "cat0.png"),)


def test_normalize_cq_string_unescapes_param_values():
    cq = "[CQ:image,file=a&amp;b.png,url=https://images.example.test/a&#44;b.png]"
    event = base_event(message=cq, raw_message=cq)
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.images == (ImageRef("https://images.example.test/a,b.png", None, "a&b.png"),)


def test_classify_and_parse_event_images_consistent_with_normalize():
    cq = f"看图[CQ:image,file=cat0.png,url={_IMAGE_URL}]"
    event = base_event(message=cq, raw_message=cq)
    normalized = normalize_onebot_event(event)
    classified = classify_onebot_event(event)
    parsed = parse_event(event)
    assert isinstance(normalized, InboundEvent)
    assert isinstance(classified, InboundEvent)
    assert classified.images == normalized.images == (ImageRef(_IMAGE_URL, None, "cat0.png"),)
    assert parsed is not None
    assert parsed.images == normalized.images


def test_normalize_cq_mid_text_codes_extract_and_strip():
    """文本中段裸写 CQ 码（两侧都有文字）的钉子（Review P3 复核）：
    _CQ_IMAGE 为无锚 finditer，旧剥离口径同样不锚定——中段 image
    码必须被提取、中段非 image 码必须被剥离、两侧文本拼接保留。"""
    cq = f"看这个[CQ:image,file=cat0.png,url={_IMAGE_URL}]是什么"
    event = base_event(message=cq, raw_message=cq)
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.text == "看这个是什么"
    assert result.images == (ImageRef(_IMAGE_URL, None, "cat0.png"),)

    plain = base_event(message="abc[CQ:at,qq=1]def", raw_message="abc[CQ:at,qq=1]def")
    stripped = normalize_onebot_event(plain)
    assert isinstance(stripped, InboundEvent)
    assert stripped.text == "abcdef"
    assert stripped.has_media is True
    assert stripped.images == ()


def test_onebot_adapter_normalize_matches_module_function():
    adapter = make_onebot(FakeOneBotClient())
    result = adapter.normalize(base_event())
    assert isinstance(result, InboundEvent)
    assert result.session_id == "onebot:90001"
    assert reason_of(adapter.normalize(base_event(message_type="group"))) == "not_private"


# ---------------------------------------------------------------------------
# normalize: the five Ignore reason codes
# ---------------------------------------------------------------------------


def test_ignore_group_message_is_not_private():
    result = normalize_onebot_event(base_event(message_type="group", group_id=80001))
    assert reason_of(result) == "not_private"
    assert "group" in str(getattr(result, "event_type_summary", ""))


def test_ignore_notice_is_not_private():
    event = {"post_type": "notice", "notice_type": "friend_add",
             "self_id": "900000001", "user_id": "90001"}
    assert reason_of(normalize_onebot_event(event)) == "not_private"


def test_ignore_missing_message_id():
    event = base_event()
    del event["message_id"]
    assert reason_of(normalize_onebot_event(event)) == "no_message_id"


def test_ignore_missing_self_id():
    event = base_event()
    del event["self_id"]
    assert reason_of(normalize_onebot_event(event)) == "no_self_id"


def test_ignore_missing_user_id_is_malformed():
    event = base_event()
    del event["user_id"]
    assert reason_of(normalize_onebot_event(event)) == "malformed"


def test_ignore_non_text_message_without_raw_is_unsupported_type():
    event = base_event(message=12345)
    del event["raw_message"]
    assert reason_of(normalize_onebot_event(event)) == "unsupported_type"


# ---------------------------------------------------------------------------
# normalize: require_keys=False relaxes only the claim keys
# ---------------------------------------------------------------------------


def test_require_keys_false_accepts_missing_message_id():
    event = base_event()
    del event["message_id"]
    result = normalize_onebot_event(event, require_keys=False)
    assert isinstance(result, InboundEvent)
    assert not result.message_id  # None or empty: no key was fabricated
    assert result.session_id == "onebot:90001"


def test_require_keys_false_accepts_missing_self_id():
    event = base_event()
    del event["self_id"]
    result = normalize_onebot_event(event, require_keys=False)
    assert isinstance(result, InboundEvent)
    assert not result.self_id


def test_require_keys_false_still_ignores_missing_user_id():
    event = base_event()
    del event["user_id"]
    assert reason_of(normalize_onebot_event(event, require_keys=False)) == "malformed"


# ---------------------------------------------------------------------------
# FakeAdapter
# ---------------------------------------------------------------------------


def test_fake_adapter_records_sends_and_returns_accepted_receipts():
    fake = make_fake()
    first = run_send(fake, text="第一条")
    second = run_send(fake, text="第二条")
    assert outcome_of(first) == "accepted"
    assert outcome_of(second) == "accepted"
    assert first.platform_message_id == "fake-1"
    assert second.platform_message_id == "fake-2"
    records = fake_sends(fake)
    assert len(records) == 2
    assert "第一条" in str(records[0]) and "90001" in str(records[0])
    assert "第二条" in str(records[1])


def test_fake_adapter_preset_rejected_receipt():
    fake = make_fake(outcome="rejected")
    receipt = run_send(fake)
    assert outcome_of(receipt) == "rejected"
    assert not receipt.platform_message_id
    assert len(fake_sends(fake)) == 1  # the attempt itself is still recorded


def test_fake_adapter_preset_ambiguous_receipt():
    fake = make_fake(outcome="ambiguous")
    receipt = run_send(fake)
    assert outcome_of(receipt) == "ambiguous"
    assert not receipt.platform_message_id


def test_fake_adapter_preset_health():
    health = make_health(online=True, good=False)
    fake = make_fake(health=health)
    result = call_health(fake)
    assert isinstance(result, AdapterHealth)
    assert bool(result.online) is True
    assert bool(result.good) is False


def test_fake_adapter_delay_seconds_delays_send():
    fake = make_fake(delay_seconds=0.2)
    started = time.monotonic()
    receipt = run_send(fake)
    elapsed = time.monotonic() - started
    assert outcome_of(receipt) == "accepted"
    assert elapsed >= 0.15  # injected delay, with scheduling slack


# ---------------------------------------------------------------------------
# OneBotAdapter.send_private outcome mapping (fake client, no network)
# ---------------------------------------------------------------------------


def test_onebot_send_private_ok_maps_to_accepted():
    client = FakeOneBotClient(result={"status": "ok", "data": {"message_id": 4242}})
    receipt = run_send(make_onebot(client), text="你好")
    assert outcome_of(receipt) == "accepted"
    assert client.calls, "adapter never reached the wrapped client"


def test_onebot_send_private_exception_maps_to_ambiguous():
    client = FakeOneBotClient(error=RuntimeError("connection lost"))
    receipt = run_send(make_onebot(client), text="你好")
    assert outcome_of(receipt) == "ambiguous"
    assert not receipt.platform_message_id


def test_onebot_send_private_rate_limited_maps_to_rejected():
    client = FakeOneBotClient(result={"status": "rate_limited"})
    receipt = run_send(make_onebot(client), text="你好")
    assert outcome_of(receipt) == "rejected"
    assert not receipt.platform_message_id


# ---------------------------------------------------------------------------
# Codex P2 review round 1 fixes (regressions for findings 1-6)
# ---------------------------------------------------------------------------


def test_ignore_summary_never_interpolates_unvalidated_type_values():
    from companion.adapters.onebot import event_type_summary

    hostile = {
        "post_type": {"message": "正文 user_id=10001"},
        "message_type": ["private", "正文"],
    }
    summary = event_type_summary(hostile)
    assert "正文" not in summary and "10001" not in summary
    assert summary == "post_type=invalid_type/message_type=invalid_type"
    normal = event_type_summary({"post_type": "message", "message_type": "group"})
    assert normal == "post_type=message/message_type=group"


@pytest.mark.parametrize(
    "bad_user",
    [[], {}, False, 0, None, ""],
    ids=["list", "dict", "bool", "zero", "none", "empty"],
)
def test_normalize_rejects_malformed_user_id_in_both_modes(bad_user):
    event = base_event()
    event["user_id"] = bad_user
    for require_keys in (True, False):
        result = normalize_onebot_event(event, require_keys=require_keys)
        assert isinstance(result, Ignore), (bad_user, require_keys)
        assert result.reason_code == "malformed"


@pytest.mark.parametrize("bad_key", [[], {}, False], ids=["list", "dict", "bool"])
def test_normalize_rejects_malformed_message_and_self_ids(bad_key):
    for field, reason in (("message_id", "no_message_id"), ("self_id", "no_self_id")):
        event = base_event()
        event[field] = bad_key
        result = normalize_onebot_event(event)
        assert isinstance(result, Ignore), (field, bad_key)
        assert result.reason_code == reason


def test_normalize_numeric_ids_are_single_normalized():
    event = base_event()
    event["message_id"] = 700001
    event["user_id"] = 90001
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.message_id == "700001"
    assert result.user_id == "90001"
    assert result.session_id == "onebot:90001"


def test_onebot_send_private_preserves_platform_message_id():
    client = FakeOneBotClient(result={"status": "ok", "data": {"message_id": 4242}})
    receipt = run_send(make_onebot(client), text="你好")
    assert outcome_of(receipt) == "accepted"
    assert receipt.platform_message_id == "4242"


@pytest.mark.parametrize("status", [[], {}, 123], ids=["list", "dict", "int"])
def test_onebot_send_private_malformed_status_is_ambiguous(status):
    client = FakeOneBotClient(result={"status": status})
    receipt = run_send(make_onebot(client), text="你好")
    assert outcome_of(receipt) == "ambiguous"


def test_onebot_send_private_translates_image_segment_to_cq():
    from companion.adapters import ImageSegment, TextSegment

    client = FakeOneBotClient()
    adapter = make_onebot(client)

    async def scenario():
        return await adapter.send_private(
            "onebot:90001",
            [TextSegment("看图"), ImageSegment({"file": "a,b.png"})],
        )

    receipt = asyncio.run(scenario())
    assert outcome_of(receipt) == "accepted"
    sent_text = client.calls[-1].get("message", "")
    assert "看图" in sent_text
    assert "[CQ:image,file=a&#44;b.png]" in sent_text


def test_onebot_send_private_untranslatable_segment_is_rejected_without_send():
    from companion.adapters import ImageSegment

    client = FakeOneBotClient()
    adapter = make_onebot(client)

    async def scenario():
        return await adapter.send_private("onebot:90001", [ImageSegment({})])

    receipt = asyncio.run(scenario())
    assert outcome_of(receipt) == "rejected"
    assert client.calls == []


def test_fake_adapter_concurrent_delayed_sends_get_unique_receipt_ids():
    adapter = FakeAdapter(delay_seconds=0.05)
    segments = [make_text_segment("你好")]

    async def scenario():
        return await asyncio.gather(
            adapter.send_private("onebot:90001", segments),
            adapter.send_private("onebot:90001", segments),
        )

    receipts = asyncio.run(scenario())
    ids = [receipt.platform_message_id for receipt in receipts]
    assert sorted(ids) == ["fake-1", "fake-2"]


# ---------------------------------------------------------------------------
# Codex P2 review round 2 fixes (regressions for findings 1-4)
# ---------------------------------------------------------------------------


def test_summary_trailing_newline_is_invalid_type():
    from companion.adapters.onebot import event_type_summary

    assert event_type_summary({"post_type": "notice\n"}) == "post_type=invalid_type"


def test_normalize_accepts_signed_message_id_but_not_signed_account_ids():
    event = base_event()
    event["message_id"] = -42
    result = normalize_onebot_event(event)
    assert isinstance(result, InboundEvent)
    assert result.message_id == "-42"
    for field in ("self_id", "user_id"):
        bad = base_event()
        bad[field] = -42
        result = normalize_onebot_event(bad)
        assert isinstance(result, Ignore), field


def test_receipt_preserves_signed_platform_message_id():
    client = FakeOneBotClient(result={"status": "ok", "data": {"message_id": -42}})
    receipt = run_send(make_onebot(client), text="你好")
    assert outcome_of(receipt) == "accepted"
    assert receipt.platform_message_id == "-42"


def test_raw_segment_type_injection_is_rejected_without_send():
    from companion.adapters import RawSegment

    client = FakeOneBotClient()
    adapter = make_onebot(client)

    async def scenario():
        return await adapter.send_private(
            "onebot:90001",
            [RawSegment("face][CQ:image,file=file:///tmp/x.png", {})],
        )

    receipt = asyncio.run(scenario())
    assert outcome_of(receipt) == "rejected"
    assert client.calls == []


def test_image_segment_keeps_extra_send_params():
    from companion.adapters import ImageSegment

    client = FakeOneBotClient()
    adapter = make_onebot(client)

    async def scenario():
        return await adapter.send_private(
            "onebot:90001",
            [ImageSegment({"file": "a.png", "type": "flash", "cache": 0})],
        )

    receipt = asyncio.run(scenario())
    assert outcome_of(receipt) == "accepted"
    sent_text = client.calls[-1].get("message", "")
    assert sent_text == "[CQ:image,file=a.png,cache=0,type=flash]"


def test_segment_with_container_param_value_is_rejected_without_send():
    from companion.adapters import ImageSegment

    client = FakeOneBotClient()
    adapter = make_onebot(client)

    async def scenario():
        return await adapter.send_private(
            "onebot:90001", [ImageSegment({"file": "a.png", "meta": {"x": 1}})]
        )

    receipt = asyncio.run(scenario())
    assert outcome_of(receipt) == "rejected"
    assert client.calls == []


def test_send_private_forwards_reservation_token():
    """DeliveryHandle 携带预留 token 时，适配器原样透传给客户端
    （额度已由 DeliveryService 预留，适配器不自行计费；P5 Review #9）。"""
    from companion.adapters.base import TextSegment

    captured = {}

    class TokenClient(FakeOneBotClient):
        async def send_private_msg(self, user_id, message, **kwargs):
            captured.update(kwargs)
            return {"status": "ok", "data": {"message_id": 4242}}

    adapter = OneBotAdapter(TokenClient())
    token = object()
    receipt = asyncio.run(
        adapter.send_private(
            "onebot:90001",
            [TextSegment("你好")],
            delivery=DeliveryHandle(priority="normal", reservation_token=token),
        )
    )
    assert receipt.outcome == "accepted"
    assert captured.get("quota_token") is token
    assert captured.get("priority") == "normal"


def test_send_private_without_token_forwards_no_quota_token():
    """无预留 token 时不得凭空造 token 传给客户端。"""
    from companion.adapters.base import TextSegment

    captured = {}

    class TokenClient(FakeOneBotClient):
        async def send_private_msg(self, user_id, message, **kwargs):
            captured.update(kwargs)
            return {"status": "ok", "data": {"message_id": 4242}}

    adapter = OneBotAdapter(TokenClient())
    receipt = asyncio.run(
        adapter.send_private(
            "onebot:90001", [TextSegment("你好")], delivery=DeliveryHandle()
        )
    )
    assert receipt.outcome == "accepted"
    assert "quota_token" not in captured
