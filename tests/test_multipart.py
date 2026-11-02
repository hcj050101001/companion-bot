"""Synthetic multipart schema, review, and provider tests."""

import asyncio
import json

import httpx
import pytest

from companion.brain import CompanionBrain, DeepSeekClient
from companion.multipart import (
    MAX_BUBBLE_CHARS,
    MAX_BUBBLES,
    MAX_ONE_BY_ONE_BUBBLES,
    MULTIPART_INSTRUCTION,
    identity_disclosure,
    join_bubbles,
    parse_bubbles,
    requests_one_by_one,
    review_bubbles,
    split_plain_text,
)


def raw(*bubbles):
    return json.dumps({"bubbles": list(bubbles)}, ensure_ascii=False)


def test_closed_schema_accepts_one_to_three_complete_bubbles():
    for count in (1, 2, 3):
        bubbles = [f"第{index}条完整的短句。" for index in range(count)]
        parsed, reason = parse_bubbles(raw(*bubbles))
        assert reason == ""
        assert parsed == bubbles
    assert join_bubbles(["一。", "二。"]) == "一。\n二。"


@pytest.mark.parametrize(
    "value,reason",
    [
        ("", "empty"),
        ("not json", "invalid_json"),
        ('```json\n{"bubbles": ["a"]}\n```', "invalid_json"),
        ('{"bubbles": ["a"], "bubbles": ["b"]}', "invalid_json"),
        ('{"bubbles": ["a"], "extra": 1}', "schema"),
        ('{"messages": ["a"]}', "schema"),
        ('{"bubbles": []}', "count"),
        ('{"bubbles": ["a", "b", "c", "d"]}', "count"),
        ('{"bubbles": ["a", 1]}', "type"),
        ('{"bubbles": ["a", ["b"]]}', "type"),
        ('{"bubbles": ["   "]}', "content"),
        ('{"bubbles": ["a\\u0000b"]}', "content"),
        ('{"bubbles": ["a\\nb"]}', "content"),
        ('{"bubbles": ["' + "a" * (MAX_BUBBLE_CHARS + 1) + '"]}', "size_or_fragment"),
        ('{"bubbles": ["同样的话。", "同样的话。"]}', "duplicate"),
        ('{"bubbles": ["名单是，"]}', "size_or_fragment"),
        ('{"bubbles": ["详见 http"]}', "size_or_fragment"),
        ('{"bubbles": ["（没有结束"]}', "size_or_fragment"),
    ],
)
def test_closed_schema_rejects_invalid_objects(value, reason):
    parsed, actual = parse_bubbles(value)
    assert parsed is None
    assert actual == reason


def test_total_size_limit_is_enforced():
    first = "啊" * (MAX_BUBBLE_CHARS - 1) + "。"
    second = "嗯" * (MAX_BUBBLE_CHARS - 1) + "。"
    third = "哦" * (MAX_BUBBLE_CHARS - 1) + "。"
    parsed, reason = parse_bubbles(raw(first, second, third))
    assert parsed is None and reason == "total_size"


def test_review_checks_every_bubble_joined_and_compact_text():
    assert review_bubbles(["我听到了。", "你想多说说吗？"]) == (True, "")
    assert review_bubbles(["我是真人，不是 AI。"]) == (False, "identity")
    assert review_bubbles(["我是", "真人。"]) == (False, "identity")
    assert review_bubbles(["你应该去死。"]) == (False, "safety")
    assert review_bubbles(["你应该去", "死。"]) == (False, "safety")
    assert review_bubbles(["我是阿年呀。"], user_text="你是谁？") == (False, "identity")
    # KD-001（P3 定版）：仅报名字+角色不算完整披露——缺当前模型名与
    # 「第三方模型」说明必须拦截换固定话术；四项齐全才放行。
    assert review_bubbles(
        ["我是阿年，一个 AI 陪伴助手。"], user_text="你是谁？"
    ) == (False, "identity")
    assert review_bubbles(
        ["我是阿年，一个 AI 陪伴助手，当前回复由第三方模型 DeepSeek 生成。"],
        user_text="你是谁？",
    ) == (True, "")


def test_fixed_identity_fallback_discloses_ai_and_model():
    line = identity_disclosure("阿年", "DeepSeek")
    assert "AI" in line and "不是真人" in line and "DeepSeek" in line
    assert review_bubbles([line], user_text="你是不是真人？") == (True, "")


def test_plain_text_splitter_accepts_blank_line_segments():
    text = "先回应你。\n\n再说第一件事。"
    assert split_plain_text(text) == ["先回应你。", "再说第一件事。"]


def test_plain_text_splitter_needs_explicit_request_for_single_newlines():
    text = "先回应你。\n再说第一件事。"
    assert split_plain_text(text) is None
    assert split_plain_text(text, one_by_one=True) == ["先回应你。", "再说第一件事。"]
    assert requests_one_by_one("请一条一条地发给我")
    assert requests_one_by_one("one by one please")
    assert not requests_one_by_one("今天有点累")


@pytest.mark.parametrize(
    "text",
    [
        "只有一条完整的回复。",
        "没有结束标点\n\n也没有结束标点",
        "他的名字是\n\n李明。",
        "详见：\n\nhttps://example.invalid/page",
        "3.14\n\n是圆周率。",
        "我是 AI 助手。\n\n有什么可以帮你？",
        "早点休息。\n\n以上仅供参考。",
        "第一段。\n\n第二\x00段。",
        "同样的话。\n\n同样的话。",
        "第一段。\n\n" + "啊" * MAX_BUBBLE_CHARS + "。",
        "```text\n第一段。\n\n第二段。\n```",
    ],
)
def test_plain_text_splitter_keeps_incomplete_or_unsafe_as_single(text):
    assert split_plain_text(text) is None


def test_plain_text_splitter_caps_at_three_bubbles_without_losing_content():
    segments = [f"第{index}条完整的短句。" for index in range(1, 5)]
    parsed = split_plain_text("\n\n".join(segments))
    assert parsed is not None
    assert len(parsed) == 3
    assert parsed[-1] == "\n".join(segments[2:])


# Synthetic shape from the real failure report: a short greeting, several short
# single-newline lines without terminal punctuation, then a short closing question.
SHORT_UNPUNCTUATED_SHAPE = (
    "嗨，你来啦\n"
    "今天过得怎么样\n"
    "我刚好想到你\n"
    "想听你说说话\n"
    "不着急慢慢讲\n"
    "晚上吃了什么呀？"
)


def test_explicit_one_by_one_short_unpunctuated_lines_stay_individual():
    # Round 7 contract (production 09b621c, ported at framework cutover):
    # an explicit one-by-one request means every complete line is delivered
    # as its own bubble (see MAX_ONE_BY_ONE_BUBBLES); the earlier grouping
    # of the tail into the third bubble silently broke that promise on a
    # real device (2026-10-31 poem report).
    assert split_plain_text(SHORT_UNPUNCTUATED_SHAPE) is None
    assert split_plain_text(SHORT_UNPUNCTUATED_SHAPE, one_by_one=True) == [
        "嗨，你来啦",
        "今天过得怎么样",
        "我刚好想到你",
        "想听你说说话",
        "不着急慢慢讲",
        "晚上吃了什么呀？",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "嗨，你来啦\n我是 AI 助手\n晚上吃什么呀？",
        "他的名字是\n李明\n今天过得不错\n想聊点什么吗？",
        "看这个\nhttps://example.invalid/page\n很好玩\n你有没有兴趣？",
        "3.14\n是圆周率\n记住了吗？",
        "早点休息\n以上仅供参考\n好吗？",
        "嗨，你来啦\n今天过得怎么样\n今天过得怎么样\n晚上吃什么呀？",
        "嗨，你来啦\n今天过得怎么样\n" + "啊" * MAX_BUBBLE_CHARS + "。",
    ],
)
def test_one_by_one_short_lines_still_refuse_unsafe_splits(text):
    assert split_plain_text(text, one_by_one=True) is None


def test_one_by_one_many_short_lines_group_within_bubble_limits():
    segments = [
        "嗨，你来啦",
        *[f"我想和你说第{index}件小事" for index in range(1, 31)],
        "你现在方便聊吗？",
    ]
    text = "\n".join(segments)
    parsed = split_plain_text(text, one_by_one=True)
    assert parsed is not None
    assert 1 < len(parsed) <= 3
    assert "\n".join(parsed) == text
    assert all(len(bubble) <= MAX_BUBBLE_CHARS for bubble in parsed)


def test_identity_disclosure_recognised_next_to_cjk_characters():
    # "AI" flanked by CJK characters has no \b boundary; it is still a
    # disclosure and must never be split, and review must still find it.
    text = "我是AI助手\n当前回复由 DeepSeek 生成"
    assert split_plain_text(text, one_by_one=True) is None
    assert split_plain_text("我是AI助手。\n\n有什么可以帮你？") is None
    # KD-001（P3 定版）：该形态缺名字/角色与「第三方模型」说明，
    # 不再算完整披露（旧口径曾放行，属有意行为变更）。
    assert review_bubbles(
        ["我是AI助手，当前回复由 DeepSeek 生成。"], user_text="你是谁？"
    ) == (False, "identity")
    assert review_bubbles(["我是阿年呀。"], user_text="你是谁？") == (
        False,
        "identity",
    )


def test_cross_segment_crisis_phrase_is_never_split():
    # "伤害自己" exists only after concatenation; neither half matches alone.
    text = "如果想伤害\n自己\n请及时求助"
    assert split_plain_text(text, one_by_one=True) is None
    assert review_bubbles(["如果想伤害", "自己请及时求助"]) == (False, "safety")
    assert review_bubbles(["我最近总是想伤害自己。"]) == (False, "safety")


@pytest.mark.parametrize(
    "text",
    [
        "金额为 3.\n14 元\n记好啦",
        "一共是 3\n点5 元\n记好啦",
        "看这个页面 https://example.invalid/page 很好玩\n今天过得不错\n晚上吃什么呀？",
        "他的名字叫作\n李明\n今天过得不错\n想聊点什么吗？",
        "His name is\nLi Ming\nNice to meet you\nHow was your day?",
        "Her name is\nAnna\nShe is nice\nDo you know her?",
        "嗨，你来啦\n¹\n晚上吃什么呀？",
        "嗨，你来啦\nⅢ\n晚上吃什么呀？",
    ],
)
def test_one_by_one_relaxed_branch_rejects_dangling_fragments(text):
    assert split_plain_text(text, one_by_one=True) is None


def test_review_rejects_english_phrases_split_across_bubbles():
    # English protected phrases need their literal spaces; the newline join
    # and the separator-free concatenation both destroy them, so review also
    # checks a space-joined variant.
    assert review_bubbles(["you should", "die"]) == (False, "safety")
    assert review_bubbles(["I am a", "human"]) == (False, "identity")
    assert split_plain_text("you should\ndie", one_by_one=True) is None
    assert split_plain_text("I am a\nhuman", one_by_one=True) is None
    # A CJK crisis phrase with a stray space inside is caught once squashed.
    assert review_bubbles(["我最近总是想伤害 自己。"]) == (False, "safety")


def test_hotline_number_needs_digit_boundaries():
    # The hotline digits inside an unrelated identifier are not crisis text.
    assert review_bubbles(["订单编号是9123567。"]) == (True, "")
    assert review_bubbles(["情况紧急时请拨打12356求助。"]) == (False, "safety")


def test_trailing_digit_lines_split_unless_a_number_continues():
    # A complete short line may end in a digit; only an actual numeric
    # continuation on the next line makes it a fragment.
    assert split_plain_text("我选方案2\n你觉得怎么样", one_by_one=True) == [
        "我选方案2",
        "你觉得怎么样",
    ]
    assert split_plain_text("圆周率大约等于3.14\n记住了吗", one_by_one=True) == [
        "圆周率大约等于3.14",
        "记住了吗",
    ]
    assert split_plain_text("金额为３．\n１４元\n记好啦", one_by_one=True) is None
    assert split_plain_text("一共是 3\n点5 元\n记好啦", one_by_one=True) is None
    # A decimal point starting the next segment continues the number too, and
    # terminal punctuation on the left does not complete a dangling quantity.
    assert split_plain_text("我有3\n.5元预算\n记好啦", one_by_one=True) is None
    assert split_plain_text("预算为3。\n14元\n记好啦", one_by_one=True) is None
    # But a punctuated digit-ending line followed by a non-numeric line splits.
    assert split_plain_text("我得了100分。\n恭喜你呀", one_by_one=True) == [
        "我得了100分。",
        "恭喜你呀",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "看这个 https:/\n/example.invalid/page 很好玩",
        "He's called\nLi Ming\nNice to meet you",
        "His name's\nLi Ming\nNice to meet you",
        "他的姓名为\n李明\n今天过得不错",
        "答案在这\n仅供 参考\n记好啦",
    ],
)
def test_one_by_one_cross_segment_variants_are_refused(text):
    assert split_plain_text(text, one_by_one=True) is None


def test_brain_asks_for_the_bounded_multipart_object():
    prompts = []

    class Client:
        async def generate_json(self, prompt):
            prompts.append(prompt)
            return raw("答案。")

    brain = CompanionBrain("synthetic persona", Client())
    assert asyncio.run(brain.respond_multipart("question", [])) == raw("答案。")
    assert MULTIPART_INSTRUCTION in prompts[0]
    assert prompts[0].endswith(MULTIPART_INSTRUCTION)


def test_deepseek_json_mode_requests_a_json_object(monkeypatch):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant",
                                          "content": raw("结构化回答。")}}]},
        )

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    assert asyncio.run(DeepSeekClient().generate_json("return JSON")) == raw("结构化回答。")
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in requests[0]


# Round 7 (production 09b621c, ported at framework cutover): real-device
# report 2026-10-31 10:13 — the user asked for a poem "一条一条背" and
# the first two bubbles arrived one by one, but the remaining lines were
# packed into a single tail bubble by _group_segments. The raw model reply
# below is the exact text recovered from production memory.db. The
# framework line has no assess_bubbles wrapper; the same three-tier
# outcomes are asserted through split_plain_text + review_bubbles, which
# are the primitives its pipeline composes.
POEM_REPLY_20261031 = (
    "好呀，一句一句来～\n"
    "日照香炉生紫烟\n"
    "遥看瀑布挂前川\n"
    "飞流直下三千尺\n"
    "疑是银河落九天\n"
    "背完啦，还想听哪首？"
)


def test_round7_one_by_one_poem_lines_each_get_their_own_bubble():
    segments = split_plain_text(POEM_REPLY_20261031, one_by_one=True)
    assert segments == POEM_REPLY_20261031.split("\n")
    assert len(segments) == 6
    assert review_bubbles(segments, user_text="你背一首古诗吧，一条一条背") == (
        True,
        "",
    )


def test_round7_one_by_one_four_poem_lines_and_closing_split_apart():
    text = (
        "日照香炉生紫烟\n"
        "遥看瀑布挂前川\n"
        "飞流直下三千尺\n"
        "疑是银河落九天\n"
        "背完啦，还想听哪首？"
    )
    segments = split_plain_text(text, one_by_one=True)
    assert segments == text.split("\n")
    assert segments[:4] == [
        "日照香炉生紫烟",
        "遥看瀑布挂前川",
        "飞流直下三千尺",
        "疑是银河落九天",
    ]
    assert segments[-1] == "背完啦，还想听哪首？"


def test_round7_one_by_one_exactly_at_cap_lines_stay_individual():
    # Pin the exact boundary: MAX_ONE_BY_ONE_BUBBLES lines each stay their
    # own bubble; one more line falls back to grouping (next test).
    lines = [f"我想和你说第{index}件小事" for index in range(1, 11)]
    assert len(lines) == MAX_ONE_BY_ONE_BUBBLES
    text = "\n".join(lines)
    assert split_plain_text(text, one_by_one=True) == lines


def test_round7_one_by_one_beyond_cap_falls_back_to_grouping():
    lines = ["嗨，你来啦"] + [
        f"我想和你说第{index}件小事" for index in range(1, 11)
    ]
    text = "\n".join(lines)
    parsed = split_plain_text(text, one_by_one=True)
    assert parsed is not None
    assert len(parsed) <= MAX_BUBBLES
    assert "\n".join(parsed) == text


@pytest.mark.parametrize(
    "text",
    [
        # A number dictated one digit per line must never become bubbles:
        # bare digit lines are not complete segments...
        "请拨打这个号码\n1\n2\n3\n5\n6",
        # ...and punctuated digit lines are a numeric continuation seam.
        "请拨打这个号码\n1。\n2。\n3。\n5。\n6。",
        "请致电求助\n1\n2\n3\n5\n6",
    ],
)
def test_round7_digits_on_their_own_lines_are_never_split(text):
    assert split_plain_text(text, one_by_one=True) is None


def test_round7_dialling_line_inside_one_by_one_answer_still_blocks():
    text = "好呀，一句一句来～\n请拨打1 2 3 5 6求助\n背完啦，还想听哪首？"
    assert split_plain_text(text, one_by_one=True) is None
    assert review_bubbles(text.split("\n"), user_text="一条一条发") == (
        False,
        "safety",
    )


def test_round7_code_blocks_and_plain_lists_are_not_split():
    code = "```python\nprint(1)\nprint(2)\n```"
    assert split_plain_text(code, one_by_one=True) is None
    # Without an explicit one-by-one request, single newlines never split,
    # so an ordinary list stays one message.
    assert split_plain_text("购物清单：\n苹果\n香蕉\n牛奶") is None
