"""N2 段全集契约（方案 §2.2 / C9 / C5(b)）：段建模、入站解析、出站
序列化（CQ 与 segment 数组双形态）、段级发送规划、出站递归复核，
以及段覆盖矩阵与实现面的一致性。

全部离线：不出网、不发真实消息。
"""

import asyncio
import json
from pathlib import Path

import pytest

from companion.adapters import (
    SUPPORTED_SEGMENT_TYPES,
    AtSegment,
    AudioSegment,
    DiceSegment,
    FaceSegment,
    FakeAdapter,
    FileSegment,
    ForwardSegment,
    ImageSegment,
    MFaceSegment,
    NodeSegment,
    OneBotAdapter,
    PokeSegment,
    RawSegment,
    ReplySegment,
    RpsSegment,
    TextSegment,
    VideoSegment,
    normalize_onebot_event,
    segments_to_onebot_message,
)
from companion.adapters.onebot import _segments_to_onebot_text
from companion.services.segments import (
    contains_media_segment,
    plan_segment_sends,
    review_outbound_segments,
    visible_texts,
)

pytestmark = pytest.mark.timeout(10)

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "napcat" / "segment_coverage.json"
)


def _event(message):
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 90001,
        "user_id": 10001,
        "message_id": 42,
        "message": message,
    }


# ------------------------------------------------------------ 入站解析


def test_parse_new_first_class_segments():
    event = _event(
        [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "reply", "data": {"id": "41"}},
            {"type": "face", "data": {"id": "14"}},
            {"type": "mface", "data": {"id": "m1"}},
            {"type": "video", "data": {"file": "v.mp4"}},
            {"type": "file", "data": {"file": "f.pdf", "name": "报告.pdf"}},
            {"type": "poke", "data": {"qq": "10001"}},
            {"type": "dice", "data": {}},
            {"type": "rps", "data": {}},
            {"type": "forward", "data": {"id": "fwd-1"}},
            {"type": "json", "data": {"data": "{}"}},
            {"type": "music", "data": {"type": "qq", "id": "123"}},
        ]
    )
    result = normalize_onebot_event(event)
    classes = [type(seg) for seg in result.segments]
    assert classes == [
        TextSegment,
        ReplySegment,
        FaceSegment,
        MFaceSegment,
        VideoSegment,
        FileSegment,
        PokeSegment,
        DiceSegment,
        RpsSegment,
        ForwardSegment,
        RawSegment,
        RawSegment,
    ]
    # 文本投影与媒体口径不变：只有 text 段进 text，非 text 即媒体。
    assert result.text == "看这个"
    assert result.has_media is True


def test_parse_node_segment_recurses_into_content():
    event = _event(
        [
            {
                "type": "node",
                "data": {
                    "user_id": "10001",
                    "nickname": "阿年",
                    "content": [
                        {"type": "text", "data": {"text": "节点里的话"}},
                        {"type": "image", "data": {"file": "a.png"}},
                    ],
                },
            }
        ]
    )
    result = normalize_onebot_event(event)
    node = result.segments[0]
    assert isinstance(node, NodeSegment)
    assert [type(seg) for seg in node.content] == [TextSegment, ImageSegment]
    assert node.content[0].text == "节点里的话"


# ------------------------------------------------------------ 出站序列化


def test_cq_serialization_golden_for_new_segments():
    cases = [
        ([TextSegment("hi"), AtSegment({"qq": 123})], "hi[CQ:at,qq=123]"),
        ([ReplySegment({"id": 42}), TextSegment("好")], "[CQ:reply,id=42]好"),
        ([FaceSegment({"id": 14})], "[CQ:face,id=14]"),
        ([MFaceSegment({"id": "m1"})], "[CQ:mface,id=m1]"),
        (
            [VideoSegment({"file": "v.mp4", "thumb": "t.jpg"})],
            "[CQ:video,file=v.mp4,thumb=t.jpg]",
        ),
        ([DiceSegment({})], "[CQ:dice]"),
        ([RpsSegment({})], "[CQ:rps]"),
        ([PokeSegment({"qq": 7})], "[CQ:poke,qq=7]"),
        (
            [TextSegment("看图"), ImageSegment({"file": "a,b.png"})],
            "看图[CQ:image,file=a&#44;b.png]",
        ),
        ([AudioSegment({"file": "a.amr", "magic": 1})], "[CQ:record,file=a.amr,magic=1]"),
    ]
    for segments, expected in cases:
        assert segments_to_onebot_message(segments) == expected, segments


def test_dispatcher_matches_legacy_cq_for_classic_segments():
    combos = [
        [TextSegment("只有文本")],
        [TextSegment("看图"), ImageSegment({"file": "a.png"})],
        [AtSegment({"qq": "all"}), TextSegment("集合")],
        [RawSegment("share", {"url": "https://example.com", "title": "t"})],
    ]
    for segments in combos:
        assert segments_to_onebot_message(segments) == _segments_to_onebot_text(segments)


def test_array_serialization_for_structured_segments():
    node = NodeSegment(
        {"user_id": 10001, "nickname": "阿年"},
        (TextSegment("节点文本"), ImageSegment({"file": "a.png"})),
    )
    message = segments_to_onebot_message([node])
    assert message == [
        {
            "type": "node",
            "data": {
                "user_id": 10001,
                "nickname": "阿年",
                "content": [
                    {"type": "text", "data": {"text": "节点文本"}},
                    {"type": "image", "data": {"file": "a.png"}},
                ],
            },
        }
    ]
    # file 段走数组（CQ 面不表达 file）。
    assert segments_to_onebot_message(
        [FileSegment({"file": "f.pdf", "name": "报告.pdf"})]
    ) == [{"type": "file", "data": {"file": "f.pdf", "name": "报告.pdf"}}]
    # 混合消息整体走数组：文本段也以数组项出现。
    assert segments_to_onebot_message(
        [TextSegment("看文件"), FileSegment({"file": "f.pdf"})]
    ) == [
        {"type": "text", "data": {"text": "看文件"}},
        {"type": "file", "data": {"file": "f.pdf"}},
    ]
    # 已知 Raw 段的标量载荷仍走 CQ（令牌校验路径，与历史口径一致）；
    # 载荷带嵌套结构时 CQ 不可表达、改走数组，载荷原样。
    assert segments_to_onebot_message(
        [RawSegment("json", {"data": "{\"a\": 1}"})]
    ) == '[CQ:json,data={"a": 1}]'
    assert segments_to_onebot_message(
        [RawSegment("music", {"type": "qq", "id": "123"})]
    ) == "[CQ:music,type=qq,id=123]"
    assert segments_to_onebot_message(
        [RawSegment("json", {"data": {"app": "x", "meta": {"a": 1}}})]
    ) == [{"type": "json", "data": {"data": {"app": "x", "meta": {"a": 1}}}}]
    # forward 引用段走数组。
    assert segments_to_onebot_message([ForwardSegment({"id": "fwd-1"})]) == [
        {"type": "forward", "data": {"id": "fwd-1"}}
    ]
    # poke 无 qq 时 CQ 不可表达、数组可表达。
    assert segments_to_onebot_message([PokeSegment({"id": 5})]) == [
        {"type": "poke", "data": {"id": 5}}
    ]


def test_unserializable_segments_return_none():
    # 缺主参数：CQ 与数组两条路都拒（不许借数组绕过）。
    assert segments_to_onebot_message([ImageSegment({})]) is None
    assert segments_to_onebot_message([ImageSegment({"file": ""})]) is None
    assert segments_to_onebot_message([ReplySegment({})]) is None
    assert segments_to_onebot_message([AtSegment({})]) is None
    # node 内容含不可序列化段 → 整体拒。
    bad_node = NodeSegment({"user_id": 1}, (ImageSegment({}),))
    assert segments_to_onebot_message([bad_node]) is None
    # 类型名不合文法（注入形态）→ 拒。
    assert (
        segments_to_onebot_message(
            [RawSegment("face][CQ:image,file=file:///tmp/x.png", {})]
        )
        is None
    )


def test_adapter_send_private_uses_array_for_node_segments():
    sent = {}

    class StubClient:
        async def send_private_msg(self, user_id, message, **kwargs):
            sent["user_id"] = user_id
            sent["message"] = message
            return {"status": "ok", "data": {"message_id": 7}}

    adapter = OneBotAdapter(client=StubClient())
    node = NodeSegment({"user_id": 1}, (TextSegment("嗨"),))
    receipt = asyncio.run(adapter.send_private("onebot:10001", [node]))
    assert receipt.outcome == "accepted"
    assert sent["message"] == [
        {
            "type": "node",
            "data": {"user_id": 1, "content": [{"type": "text", "data": {"text": "嗨"}}]},
        }
    ]


# ------------------------------------------------------------ 覆盖一致性


_FIRST_CLASS = {
    "text": TextSegment,
    "at": AtSegment,
    "reply": ReplySegment,
    "face": FaceSegment,
    "mface": MFaceSegment,
    "image": ImageSegment,
    "record": AudioSegment,
    "video": VideoSegment,
    "file": FileSegment,
    "poke": PokeSegment,
    "dice": DiceSegment,
    "rps": RpsSegment,
    "forward": ForwardSegment,
    "node": NodeSegment,
}

_SAMPLE_DATA = {
    "text": {"text": "x"},
    "at": {"qq": "1"},
    "reply": {"id": "1"},
    "face": {"id": "1"},
    "mface": {"id": "1"},
    "image": {"file": "a.png"},
    "record": {"file": "a.amr"},
    "video": {"file": "v.mp4"},
    "file": {"file": "f.pdf"},
    "poke": {"qq": "1"},
    "dice": {},
    "rps": {},
    "forward": {"id": "fwd"},
    "node": {"user_id": "1", "content": [{"type": "text", "data": {"text": "x"}}]},
}


def test_segment_matrix_matches_implementation_surface():
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
    by_type = {row["type"]: row for row in rows}
    # 待验 2 类（flashtransfer/online_file）不在实现面内，其余全在。
    for seg_type, row in by_type.items():
        if row["verification"] == "schema_unverified":
            assert seg_type not in SUPPORTED_SEGMENT_TYPES
        else:
            assert seg_type in SUPPORTED_SEGMENT_TYPES
    # 一等段逐一可解析为对应段类；已知 Raw 段解析为 RawSegment。
    for seg_type, cls in _FIRST_CLASS.items():
        result = normalize_onebot_event(
            _event([{"type": seg_type, "data": _SAMPLE_DATA[seg_type]}])
        )
        assert isinstance(result.segments[0], cls), seg_type
    for seg_type in ("json", "xml", "music", "contact", "location", "markdown", "miniapp"):
        result = normalize_onebot_event(
            _event([{"type": seg_type, "data": {"data": "x"}}])
        )
        assert isinstance(result.segments[0], RawSegment), seg_type
    # 适配器能力声明与实现面同源。
    assert FakeAdapter().capabilities.segment_types == SUPPORTED_SEGMENT_TYPES
    assert OneBotAdapter().capabilities.segment_types == SUPPORTED_SEGMENT_TYPES


# ------------------------------------------------------------ 段级规划


def test_plan_splits_only_text_runs_and_keeps_media_atomic():
    image = ImageSegment({"file": "a.png"})
    units = plan_segment_sends(
        [TextSegment("第一段。\n\n第二段。"), image, TextSegment("尾巴。")],
        split_text=True,
    )
    assert len(units) == 4
    assert units[0] == (TextSegment("第一段。"),)
    assert units[1] == (TextSegment("第二段。"),)
    assert units[2] == (image,)
    assert units[3] == (TextSegment("尾巴。"),)


def test_plan_group_mode_never_splits_text():
    units = plan_segment_sends(
        [TextSegment("第一段。\n\n第二段。")], split_text=False
    )
    assert units == [(TextSegment("第一段。\n\n第二段。"),)]


def test_plan_unsplittable_text_stays_single_unit():
    code = TextSegment("```python\nprint(1)\n```")
    assert plan_segment_sends([code], split_text=True) == [(code,)]


def test_contains_media_segment_recurses_into_nodes():
    assert contains_media_segment([TextSegment("x")]) is False
    assert contains_media_segment([RawSegment("json", {"data": "{}"})]) is True
    node = NodeSegment({"user_id": 1}, (TextSegment("x"), FileSegment({"file": "f"})))
    assert contains_media_segment([node]) is True
    clean_node = NodeSegment({"user_id": 1}, (TextSegment("x"),))
    assert contains_media_segment([clean_node]) is False


# ------------------------------------------------------------ 递归复核


def test_review_passes_clean_segments():
    assert review_outbound_segments([TextSegment("今天天气不错")]) == (True, "")
    card = RawSegment(
        "json", {"data": json.dumps({"title": "天气", "desc": "今天晴"})}
    )
    assert review_outbound_segments([card]) == (True, "")


def test_review_blocks_identity_denial_in_node_content():
    node = NodeSegment({"user_id": 1}, (TextSegment("我是一个真人"),))
    allowed, reason = review_outbound_segments([node])
    assert allowed is False and reason == "identity"


def test_review_blocks_harmful_text_in_json_card():
    card = RawSegment(
        "json", {"data": json.dumps({"title": "点我", "desc": "去死吧"})}
    )
    allowed, reason = review_outbound_segments([card])
    assert allowed is False and reason == "safety"


def test_review_blocks_crisis_phrase_in_file_name():
    seg = FileSegment({"file": "/data/files/自杀指南.pdf"})
    allowed, reason = review_outbound_segments([seg])
    assert allowed is False and reason == "safety"


def test_visible_texts_extracts_forward_raw_structure():
    seg = ForwardSegment(
        {
            "id": "fwd",
            "content": [
                {"type": "text", "data": {"text": "转发里的文本"}},
            ],
        }
    )
    assert visible_texts([seg]) == ["转发里的文本"]
