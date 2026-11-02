"""图片理解接缝回归（2026-10-31 真机不触发缺陷）。

与 test_vision_images.py 的单元/替身用例互补：本文件全部用例都从
**原始 OneBot 报文**出发，经 normalize/classify 与 ingress 的真实
路径（handle 与 webhook 两条入口），视觉服务只在最末端以 Fake
接管——报文 → 段 → 图片引用 → 视觉接管的衔接不再允许替身绕过。
报文形态与真机 NapCat 4.18.41 实测键形态对齐（段数组 image data
含 summary/sub_type/file_size 字符串；CQ 形 message 即 CQ 串）。
"""

import asyncio
import json

import httpx
import pytest

from companion.vision import MEDIA_FIXED_REPLY, PreparedImages, VisionService
from tests import test_app
from tests.fake_onebot import private_event

setup_vision_app = test_app.setup_app

pytestmark = pytest.mark.timeout(10)

IMAGE_URL = "https://images.example.test/cat0.png"
UPPERCASE_SCHEME_IMAGE_URL = "HTTPS://images.example.test/cat0.png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
DATA_URL_PREFIX = "data:image/png;base64,"

FAITHFUL_IMAGE_SEGMENT = {
    "type": "image",
    "data": {
        "summary": "",
        "file": "cat0.png",
        "sub_type": 0,
        "url": IMAGE_URL,
        "file_size": "12345",
    },
}
CQ_PURE = f"[CQ:image,file=cat0.png,url={IMAGE_URL}]"
CQ_MIXED = f"这是什么[CQ:image,file=cat0.png,url={IMAGE_URL}]"


class FakeVisionService:
    """最末端接管：记录 prepare 收到的引用，产出罐装视觉回复。

    describe 为内容筛查（Review F7）的描述替身：默认返回良性
    描述（过测试危机网关判 clear），不影响本文件的接缝断言。
    """

    def __init__(self, reply="看到啦，是一只猫"):
        self.reply = reply
        self.prepare_calls = []
        self.generate_calls = []
        self.describe_calls = []

    async def prepare(self, refs):
        refs = tuple(refs)
        self.prepare_calls.append(refs)
        return PreparedImages(
            data_urls=tuple(f"{DATA_URL_PREFIX}AAAA" for _ in refs),
            received=len(refs),
            skipped=0,
        )

    async def generate(self, prompt, data_urls):
        self.generate_calls.append((prompt, tuple(data_urls)))
        return self.reply

    async def describe(self, data_urls):
        self.describe_calls.append(tuple(data_urls))
        return "图片里是一只猫"


class FakeFetcher:
    def __init__(self):
        self.refs = []

    async def fetch(self, ref, *, max_bytes):
        self.refs.append(ref)
        return PNG_BYTES


class FakeVisionClient:
    def __init__(self):
        self.prompts = []

    def has_credentials(self):
        return True

    async def generate_with_images(self, prompt, image_data_urls):
        self.prompts.append((prompt, tuple(image_data_urls)))
        return "看到啦，是一只猫"


def _ready_processor(app, service):
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.vision_service = service
    return processor


def _array_event(text=None, *, message_id="seam-array-1"):
    segments = []
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    segments.append(dict(FAITHFUL_IMAGE_SEGMENT))
    event = private_event("10001", text or "")
    event["message"] = segments
    event["message_id"] = message_id
    event["raw_message"] = (text or "") + "[CQ:image,file=cat0.png]"
    return event


def _cq_event(cq, *, message_id="seam-cq-1"):
    event = private_event("10001", "")
    event["message"] = cq
    event["raw_message"] = cq
    event["message_id"] = message_id
    return event


def _messages(calls):
    return [call["message"] for call in calls]


# ---------------------------------------------------------------------------
# handle() 真实路径：原始报文 → dispatch 组装 → 视觉接管
# ---------------------------------------------------------------------------


def test_handle_array_pure_image_reaches_vision(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_array_event()))
    assert _messages(calls) == ["看到啦，是一只猫"]
    (refs,) = service.prepare_calls
    assert [ref.ref for ref in refs] == [IMAGE_URL]


def test_handle_array_mixed_reaches_vision(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_array_event("这是什么")))
    assert _messages(calls) == ["看到啦，是一只猫"]
    prompt, _ = service.generate_calls[0]
    assert "这是什么" in prompt


def test_handle_cq_pure_image_reaches_vision(setup_vision_app):
    """CQ 形纯图：修复前恒回固定文案（引用在解析期被丢弃）。"""
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_cq_event(CQ_PURE)))
    assert _messages(calls) == ["看到啦，是一只猫"]
    assert MEDIA_FIXED_REPLY not in _messages(calls)
    (refs,) = service.prepare_calls
    assert [ref.ref for ref in refs] == [IMAGE_URL]


def test_handle_cq_mixed_reaches_vision(setup_vision_app):
    """CQ 形图文：修复前回落普通文本路、视觉从不被调用。"""
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_cq_event(CQ_MIXED)))
    assert _messages(calls) == ["看到啦，是一只猫"]
    prompt, _ = service.generate_calls[0]
    assert "这是什么" in prompt


def test_handle_real_vision_service_fetches_ref_and_generates(setup_vision_app):
    """真 VisionService 接缝版：取图器必须收到 normalize 提取的引用，
    生成端必须收到 data URL——防 Fake 服务罐装结果掩盖接缝（双形态）。"""
    app, calls, _, _ = setup_vision_app
    fetcher = FakeFetcher()
    client = FakeVisionClient()
    service = VisionService(client=client, fetcher=fetcher)
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_array_event(message_id="seam-real-1")))
    assert asyncio.run(processor.handle(_cq_event(CQ_PURE, message_id="seam-real-2")))
    assert _messages(calls) == ["看到啦，是一只猫", "看到啦，是一只猫"]
    assert fetcher.refs == [IMAGE_URL, IMAGE_URL]
    assert all(
        data_urls and data_urls[0].startswith(DATA_URL_PREFIX)
        for _, data_urls in client.prompts
    )


# ---------------------------------------------------------------------------
# webhook 真实路径：验签 → Stage 0 → 事件道 → dispatch → 视觉接管
# ---------------------------------------------------------------------------


async def _post_and_drain(app, event):
    body = json.dumps(event, ensure_ascii=False).encode()
    consumer = asyncio.create_task(app.state.event_consumer())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/onebot/event",
                content=body,
                headers={"X-Signature": test_app.signed(body)},
            )
        assert response.status_code == 204
        await test_app.drain(app)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.parametrize(
    "make_event",
    [
        lambda: _array_event(message_id="seam-hook-array"),
        lambda: _cq_event(CQ_PURE, message_id="seam-hook-cq"),
    ],
    ids=["array", "cq"],
)
def test_webhook_pure_image_reaches_vision(setup_vision_app, make_event):
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    _ready_processor(app, service)

    asyncio.run(_post_and_drain(app, make_event()))
    assert _messages(calls) == ["看到啦，是一只猫"]
    assert MEDIA_FIXED_REPLY not in _messages(calls)
    (refs,) = service.prepare_calls
    assert [ref.ref for ref in refs] == [IMAGE_URL]


# ---------------------------------------------------------------------------
# ImageRef.file_id 契约（NapCat 介导取图轮）：原始报文 → normalize →
# parsed.images，file_id 必须钉住段 file 值、ref 仍为 url
# ---------------------------------------------------------------------------


def _file_only_array_event(*, message_id="seam-file-only-array"):
    event = private_event("10001", "")
    event["message"] = [
        {
            "type": "image",
            "data": {"summary": "", "file": "onlyfile.jpg", "sub_type": 0, "file_size": "999"},
        }
    ]
    event["message_id"] = message_id
    event["raw_message"] = "[CQ:image,file=onlyfile.jpg]"
    return event


def test_contract_array_segment_file_id_from_file_key():
    from companion.perception import parse_event

    parsed = parse_event(_array_event(message_id="seam-contract-array"))
    assert parsed is not None
    assert len(parsed.images) == 1
    assert parsed.images[0].file_id == "cat0.png"
    assert parsed.images[0].ref == IMAGE_URL


def test_contract_cq_segment_file_id_from_file_param():
    from companion.perception import parse_event

    parsed = parse_event(_cq_event(CQ_PURE, message_id="seam-contract-cq"))
    assert parsed is not None
    assert len(parsed.images) == 1
    assert parsed.images[0].file_id == "cat0.png"
    assert parsed.images[0].ref == IMAGE_URL


def test_contract_file_only_segment_ref_falls_back_to_file():
    from companion.perception import parse_event

    parsed = parse_event(_file_only_array_event())
    assert parsed is not None
    assert len(parsed.images) == 1
    assert parsed.images[0].ref == "onlyfile.jpg"
    assert parsed.images[0].file_id == "onlyfile.jpg"


def test_contract_cq_file_only_ref_falls_back_to_file():
    from companion.perception import parse_event

    parsed = parse_event(
        _cq_event("[CQ:image,file=onlyfile.jpg]", message_id="seam-contract-cq-file-only")
    )
    assert parsed is not None
    assert len(parsed.images) == 1
    assert parsed.images[0].ref == "onlyfile.jpg"
    assert parsed.images[0].file_id == "onlyfile.jpg"


# ---------------------------------------------------------------------------
# 纯图准入（确认复审 P2-2）：file-only 纯图在 NapCat 介导取图已
# 接线时必须延期到视觉路由；未接线时仍回原固定文案（旧口径）
# ---------------------------------------------------------------------------


def test_handle_file_only_pure_image_deferred_when_napcat_wired(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    service.napcat_fetcher = object()  # 已接线标记（鸭子：非 None 即可）
    processor = _ready_processor(app, service)

    assert asyncio.run(processor.handle(_file_only_array_event()))
    assert _messages(calls) == ["看到啦，是一只猫"]
    (refs,) = service.prepare_calls
    assert [ref.file_id for ref in refs] == ["onlyfile.jpg"]


def test_handle_cq_file_only_pure_image_deferred_when_napcat_wired(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    service.napcat_fetcher = object()
    processor = _ready_processor(app, service)

    event = _cq_event("[CQ:image,file=onlyfile.jpg]", message_id="seam-fileonly-cq")
    assert asyncio.run(processor.handle(event))
    assert _messages(calls) == ["看到啦，是一只猫"]
    (refs,) = service.prepare_calls
    assert [ref.file_id for ref in refs] == ["onlyfile.jpg"]


def test_handle_file_only_pure_image_fixed_reply_without_napcat(setup_vision_app):
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()  # 无 napcat_fetcher 属性 = 未接线
    processor = _ready_processor(app, service)

    event = _file_only_array_event(message_id="seam-fileonly-control")
    asyncio.run(processor.handle(event))
    assert _messages(calls) == [MEDIA_FIXED_REPLY]
    assert service.prepare_calls == []


def test_handle_pure_image_uppercase_scheme_deferred_to_vision(setup_vision_app):
    """纯图 ref 为大写 scheme（HTTPS://）：准入须大小写不敏感。

    下层 urlparse / ResourceResolver / 校验器均大小写不敏感，准入层
    若大小写敏感会把可取的纯图误拒为固定媒体文案（审查 P4）。
    """
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    processor = _ready_processor(app, service)

    event = private_event("10001", "")
    event["message"] = [
        {
            "type": "image",
            "data": {
                **FAITHFUL_IMAGE_SEGMENT["data"],
                "url": UPPERCASE_SCHEME_IMAGE_URL,
            },
        }
    ]
    event["message_id"] = "seam-uppercase-scheme"
    event["raw_message"] = "[CQ:image,file=cat0.png]"

    assert asyncio.run(processor.handle(event))
    assert _messages(calls) == ["看到啦，是一只猫"]
    assert MEDIA_FIXED_REPLY not in _messages(calls)
    (refs,) = service.prepare_calls
    assert [ref.ref for ref in refs] == [UPPERCASE_SCHEME_IMAGE_URL]


def test_webhook_group_image_stays_observe_only(setup_vision_app):
    """群图片仍只登记不回复：images 契约字段不改变群子链行为。"""
    app, calls, _, _ = setup_vision_app
    service = FakeVisionService()
    _ready_processor(app, service)
    event = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "self_id": "900000001",
        "user_id": "10001",
        "group_id": "70001",
        "message_id": "seam-group-1",
        "sender": {"user_id": 10001, "nickname": "probe"},
        "message": [dict(FAITHFUL_IMAGE_SEGMENT)],
        "raw_message": "[CQ:image,file=cat0.png]",
    }

    asyncio.run(_post_and_drain(app, event))
    assert calls == []
    assert service.prepare_calls == []
