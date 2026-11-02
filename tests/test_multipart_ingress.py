"""Synthetic reply-path tests for multipart validation, fallback, and quota."""

import asyncio
import json

from companion.brain import FALLBACK_REPLY, CompanionBrain
from companion.ingress import EventProcessor
from companion.quota import NORMAL_PER_USER_LIMIT, QuotaUnavailable, SendQuota
from companion.safety import crisis_reply
from companion.tools.read_only import ReadOnlyRegistry, ReadOnlyTool
from tests import test_app
from tests.fake_onebot import private_event

setup_multipart_app = test_app.setup_app


def raw(*bubbles):
    return json.dumps({"bubbles": list(bubbles)}, ensure_ascii=False)


SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 30}},
    "required": ["query"],
    "additionalProperties": False,
}


def tool_registry():
    async def action(query):
        return "synthetic-tool-result"

    instance = ReadOnlyRegistry(
        enabled=True, policies={"lookup": {"enabled": True, "rate_limit": 10}}
    )
    instance.register(ReadOnlyTool("lookup", "Synthetic lookup", SCHEMA, action))
    return instance


def plain_tool_brain(processor, monkeypatch, reply, *, tools_used=False):
    """Make the tool-offering route return a plain-text answer without tool calls."""

    async def respond_with_tools(*_args, **_kwargs):
        return reply, tools_used

    monkeypatch.setattr(processor.brain, "respond_with_tools", respond_with_tools)


class LLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def generate_json(self, _prompt):
        self.calls += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_invalid_structured_output_uses_one_fixed_reply_without_retry(setup_multipart_app):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    llm = LLM(["not json"])
    processor.brain = CompanionBrain(settings.persona, llm)

    assert asyncio.run(processor.handle(private_event(text="synthetic question")))
    assert llm.calls == 1
    assert calls[-1]["message"] == FALLBACK_REPLY
    history = asyncio.run(processor.conversation.get_history("10001"))
    assert history[-1] == ("assistant", FALLBACK_REPLY)


def test_identity_denial_falls_back_to_reviewed_disclosure(setup_multipart_app):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.brain = CompanionBrain(
        settings.persona, LLM([raw("我是真人，不是人工智能。")])
    )

    assert asyncio.run(processor.handle(private_event(text="你是谁")))
    assert calls[-1]["message"] == processor.identity_fallback
    assert "我是真人" not in calls[-1]["message"]
    assert "AI" in calls[-1]["message"] and "第三方模型" in calls[-1]["message"]


def test_identity_question_with_invalid_output_still_gets_disclosure(setup_multipart_app):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.brain = CompanionBrain(settings.persona, LLM(["not json"]))

    assert asyncio.run(processor.handle(private_event(text="你是谁")))
    assert calls[-1]["message"] == processor.identity_fallback
    assert "第三方模型" in calls[-1]["message"]


def test_valid_three_bubbles_are_sent_in_order_and_stored_as_accepted(setup_multipart_app):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    bubbles = ["先回应你。", "再说第一件事。", "最后提醒一句。"]
    processor.brain = CompanionBrain(settings.persona, LLM([raw(*bubbles)]))

    assert asyncio.run(processor.handle(private_event(text="synthetic request")))
    assert [call["message"] for call in calls[-3:]] == bubbles
    assert asyncio.run(processor.conversation.get_history("10001")) == [
        ("user", "synthetic request"),
        ("assistant", "\n".join(bubbles)),
    ]


def test_partial_capacity_sends_one_joined_complete_answer(setup_multipart_app):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.send_quota = SendQuota(normal_per_user=2)
    bubbles = ["第一条完整回答。", "第二条完整回答。", "第三条完整回答。"]
    processor.brain = CompanionBrain(settings.persona, LLM([raw(*bubbles)]))

    assert asyncio.run(processor.handle(private_event(text="synthetic request")))
    assert len(calls) == 1
    assert calls[0]["message"] == "\n".join(bubbles)
    history = asyncio.run(processor.conversation.get_history("10001"))
    assert history[-1] == ("assistant", "\n".join(bubbles))


def test_zero_capacity_sends_nothing_and_logs_content_free_rate_limit(setup_multipart_app):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.send_quota = SendQuota(normal_per_user=0)
    llm = LLM([raw("第一条。", "第二条。")])
    processor.brain = CompanionBrain(settings.persona, llm)

    assert not asyncio.run(processor.handle(private_event(text="synthetic request")))
    assert calls == []
    assert llm.calls == 1  # one generation, no retry and no queued later reply
    records = [json.loads(line) for line in processor.log_path.read_text().splitlines()]
    assert records[-1]["event_type"] == "rate_limited"
    assert "synthetic request" not in processor.log_path.read_text()


def test_unavailable_shared_accounting_fails_closed(setup_multipart_app, monkeypatch):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.brain = CompanionBrain(settings.persona, LLM([raw("回答。")]))

    def unavailable(*_args, **_kwargs):
        raise QuotaUnavailable("synthetic")

    monkeypatch.setattr(processor.send_quota, "reserve", unavailable)
    assert not asyncio.run(processor.handle(private_event(text="synthetic request")))
    assert calls == []
    records = [json.loads(line) for line in processor.log_path.read_text().splitlines()]
    assert records[-1]["event_type"] == "rate_limited"


def test_event_processor_shares_one_quota_and_raises_normal_policy(setup_multipart_app):
    _, _, _, settings = setup_multipart_app
    processor = EventProcessor(settings)
    assert processor.sender.quota is processor.send_quota
    assert (
        processor.sender.gateway.policies["send_private_msg"]["rate_limit"]
        == NORMAL_PER_USER_LIMIT
    )


def test_tool_path_plain_text_paragraphs_split_into_separate_sends(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, "先回应你。\n\n再说第一件事。")

    assert asyncio.run(processor.handle(private_event(text="synthetic question")))
    assert [call["message"] for call in calls] == ["先回应你。", "再说第一件事。"]
    assert asyncio.run(processor.conversation.get_history("10001"))[-1] == (
        "assistant",
        "先回应你。\n再说第一件事。",
    )


def test_explicit_one_by_one_request_splits_single_newlines(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, "先回应你。\n再说第一件事。")

    assert asyncio.run(processor.handle(private_event(text="请一条一条地发给我")))
    assert [call["message"] for call in calls] == ["先回应你。", "再说第一件事。"]


def test_plain_text_single_newlines_stay_single_without_request(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    revision = "先回应你。\n再说第一件事。"
    plain_tool_brain(processor, monkeypatch, revision)

    assert asyncio.run(processor.handle(private_event(text="synthetic question")))
    assert [call["message"] for call in calls] == [revision]


def test_short_single_segment_plain_reply_stays_single(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, "我在，慢慢说。")

    assert asyncio.run(processor.handle(private_event(text="synthetic question")))
    assert [call["message"] for call in calls] == ["我在，慢慢说。"]


def test_multipart_path_plain_text_paragraphs_split_instead_of_falling_back(
    setup_multipart_app,
):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.brain = CompanionBrain(
        settings.persona, LLM(["先回应你。\n\n再说第一件事。"])
    )

    assert asyncio.run(processor.handle(private_event(text="synthetic question")))
    assert [call["message"] for call in calls] == ["先回应你。", "再说第一件事。"]


def test_unsafe_plain_text_split_falls_back_to_reviewed_single_reply(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, "我是真人。\n\n今天天气不错。")

    assert asyncio.run(processor.handle(private_event(text="synthetic question")))
    assert [call["message"] for call in calls] == [processor.identity_fallback]


def test_tool_assisted_multisegment_answer_stays_one_bubble(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    answer = "第一段。\n\n第二段。"
    plain_tool_brain(processor, monkeypatch, answer, tools_used=True)

    assert asyncio.run(processor.handle(private_event(text="synthetic question")))
    assert [call["message"] for call in calls] == [answer]


def test_onboarding_remains_one_single_send(setup_multipart_app):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    processor.limiter.limit = 50

    assert asyncio.run(processor.handle(private_event(text="synthetic first")))
    assert calls[0]["message"] == processor._onboarding_text()
    assert len(calls) == 2


def test_crisis_and_forget_remain_single_sends(setup_multipart_app, monkeypatch):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, "第一段。\n\n第二段。")

    assert asyncio.run(processor.handle(private_event(text="想自杀")))
    assert [call["message"] for call in calls] == [
        crisis_reply(settings.safety["helplines"], False, settings.safety)
    ]
    calls.clear()
    assert asyncio.run(processor.handle(private_event(text="忘掉我")))
    assert [call["message"] for call in calls] == ["好的，我已删除保存的短期记忆。"]


SHORT_UNPUNCTUATED_SHAPE = (
    "嗨，你来啦\n"
    "今天过得怎么样\n"
    "我刚好想到你\n"
    "想听你说说话\n"
    "不着急慢慢讲\n"
    "晚上吃了什么呀？"
)
SHORT_UNPUNCTUATED_BUBBLES = [
    "嗨，你来啦",
    "今天过得怎么样",
    "我刚好想到你",
    "想听你说说话",
    "不着急慢慢讲",
    "晚上吃了什么呀？",
]


def test_explicit_one_by_one_short_unpunctuated_lines_deliver_each_line(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, SHORT_UNPUNCTUATED_SHAPE)

    assert asyncio.run(processor.handle(private_event(text="你一条一条发给我好不好")))
    assert [call["message"] for call in calls] == SHORT_UNPUNCTUATED_BUBBLES
    assert len(calls) == 6
    assert asyncio.run(processor.conversation.get_history("10001"))[-1] == (
        "assistant",
        SHORT_UNPUNCTUATED_SHAPE,
    )


def test_multipart_path_one_by_one_short_unpunctuated_lines_split(
    setup_multipart_app,
):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.brain = CompanionBrain(
        settings.persona, LLM([SHORT_UNPUNCTUATED_SHAPE])
    )

    assert asyncio.run(processor.handle(private_event(text="你一条一条发给我好不好")))
    assert [call["message"] for call in calls] == SHORT_UNPUNCTUATED_BUBBLES


# Round 7 real-device regression (production 09b621c, ported at cutover;
# 2026-10-31 10:13): the exact plain-text reply recovered from production
# memory for "你背一首古诗吧，一条一条背" must be delivered as six
# separate sends, not two sends plus a packed tail.
POEM_REPLY_20261031 = (
    "好呀，一句一句来～\n"
    "日照香炉生紫烟\n"
    "遥看瀑布挂前川\n"
    "飞流直下三千尺\n"
    "疑是银河落九天\n"
    "背完啦，还想听哪首？"
)


def test_round7_one_by_one_poem_reply_delivers_six_sends(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, POEM_REPLY_20261031)

    assert asyncio.run(
        processor.handle(private_event(text="你背一首古诗吧，一条一条背"))
    )
    assert [call["message"] for call in calls] == POEM_REPLY_20261031.split("\n")
    assert len(calls) == 6
    assert asyncio.run(processor.conversation.get_history("10001"))[-1] == (
        "assistant",
        POEM_REPLY_20261031,
    )


def test_short_unpunctuated_lines_stay_single_without_explicit_request(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, SHORT_UNPUNCTUATED_SHAPE)

    assert asyncio.run(processor.handle(private_event(text="synthetic question")))
    assert [call["message"] for call in calls] == [SHORT_UNPUNCTUATED_SHAPE]


def test_one_by_one_short_single_segment_stays_single(setup_multipart_app, monkeypatch):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, "我在呢")

    assert asyncio.run(processor.handle(private_event(text="一条一条发给我")))
    assert [call["message"] for call in calls] == ["我在呢"]


def test_one_by_one_short_lines_tool_assisted_answer_stays_one_bubble(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(
        processor, monkeypatch, SHORT_UNPUNCTUATED_SHAPE, tools_used=True
    )

    assert asyncio.run(processor.handle(private_event(text="你一条一条发给我好不好")))
    assert [call["message"] for call in calls] == [SHORT_UNPUNCTUATED_SHAPE]


def test_one_by_one_identity_denial_falls_back_to_one_disclosure(
    setup_multipart_app, monkeypatch
):
    app, calls, _, _ = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(
        processor, monkeypatch, "我是真人\n今天天气不错\n想聊点什么吗？"
    )

    assert asyncio.run(processor.handle(private_event(text="你一条一条发给我好不好")))
    assert [call["message"] for call in calls] == [processor.identity_fallback]


def test_one_by_one_short_lines_crisis_stays_one_fixed_reply(
    setup_multipart_app, monkeypatch
):
    app, calls, _, settings = setup_multipart_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.read_only_registry = tool_registry()
    plain_tool_brain(processor, monkeypatch, SHORT_UNPUNCTUATED_SHAPE)

    assert asyncio.run(processor.handle(private_event(text="想自杀")))
    assert [call["message"] for call in calls] == [
        crisis_reply(settings.safety["helplines"], False, settings.safety)
    ]
