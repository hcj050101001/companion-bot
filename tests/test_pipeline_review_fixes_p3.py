"""Codex P3 Review 第 1 轮修复的回归测试（协调者裁决后落地）。

覆盖：#1 critical 专职池调度隔离、#2 同步危机兜底失效旧普通回复、
#3 关键词路径不等待 judge 许可、#4 工具合成回复身份复核、
#5 claim 完成映射复合键、#6 身份 AI 项独立性、#7 身份问法覆盖与
英文误触发、#9 队列满时重复投递仍 204。
"""

import asyncio
import time

import pytest

from companion.multipart import is_identity_question, review_bubbles
from companion.safety import crisis_reply
from tests.fake_onebot import private_event
from tests.test_adapter_contract_p0 import fake_event
from tests.test_pipeline_saturation_p3 import build_app, messages, post_status


async def _start_consumers(app):
    task = await app.state.start_consumer()
    return task, app.state.critical_consumer_task


async def _stop_consumers(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# #6 / #7 身份披露契约（multipart 纯函数级）
# ---------------------------------------------------------------------------


def test_ai_item_is_independent_of_third_party_item():
    # 只报名字+模型处理、未披露自身为 AI：四项看似齐三项，必须拦截。
    assert review_bubbles(
        ["我是阿年。对话由第三方模型 DeepSeek 处理。"], user_text="你是谁"
    ) == (False, "identity")
    # 自身 AI 身份独立成立时放行。
    assert review_bubbles(
        ["我是阿年，一个 AI 陪伴助手。当前回复由第三方模型 DeepSeek 生成。"],
        user_text="你是谁",
    ) == (True, "")


@pytest.mark.parametrize(
    "question",
    ["你叫什么名字", "你背后的模型是什么", "对话会发给别人吗", "What model are you?"],
)
def test_new_identity_question_forms_trigger(question):
    assert is_identity_question(question)


@pytest.mark.parametrize(
    "question",
    ["Which model are you recommending for beginners?", "这个模型怎么做", "今天天气怎么样"],
)
def test_non_identity_questions_do_not_trigger(question):
    assert not is_identity_question(question)


# ---------------------------------------------------------------------------
# #3 关键词路径不等待 judge 许可；普通文本等待超时按疑似危机
# ---------------------------------------------------------------------------


def test_keyword_crisis_bypasses_judge_semaphore(tmp_path, monkeypatch):
    app, _sender, _settings = build_app(tmp_path, monkeypatch)
    processor = app.state.processor
    processor._judge_wait_seconds = 0.3

    async def scenario():
        await processor._judge_semaphore.acquire()
        await processor._judge_semaphore.acquire()
        started = time.monotonic()
        result = await processor._detect_with_budget("想自杀")
        elapsed = time.monotonic() - started
        assert result == (True, "明确自伤计划")
        assert elapsed < 0.25  # 未等满等待截止，分类未降级为「疑似危机」
        started = time.monotonic()
        result = await processor._detect_with_budget("今天心情一般")
        elapsed = time.monotonic() - started
        assert result == (True, "疑似危机")  # 等待超时按 D1 保守口径
        assert elapsed >= 0.25

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #2 同步危机兜底先失效旧普通回复
# ---------------------------------------------------------------------------


def test_emergency_crisis_invalidates_ordinary_stream(tmp_path, monkeypatch):
    app, sender, settings = build_app(tmp_path, monkeypatch)
    processor = app.state.processor

    async def scenario():
        async def forever():
            await asyncio.sleep(60)

        stream = asyncio.create_task(forever())
        processor._ordinary_streams["10001"] = stream
        epoch_before = processor._safety_epoch["10001"]
        await processor.send_emergency_crisis("10001")
        await asyncio.gather(stream, return_exceptions=True)
        assert stream.cancelled()
        assert processor._safety_epoch["10001"] == epoch_before + 1
        assert messages(sender) == [
            crisis_reply(settings.safety["helplines"], False, settings.safety)
        ]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #4 工具实际调用后的合成回复过身份复核
# ---------------------------------------------------------------------------


def test_tool_synthesized_identity_reply_is_replaced(tmp_path, monkeypatch):
    app, sender, _settings = build_app(tmp_path, monkeypatch)
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.read_only_registry.schemas = lambda: [{"type": "function"}]

    class StubBrain:
        async def respond_with_tools(self, *_args, **_kwargs):
            return "我是 AI 助手。", True

    processor.brain = StubBrain()

    async def scenario():
        assert await processor.handle(private_event("10001", "你是谁"))
        assert messages(sender) == [processor.identity_fallback]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #9 队列满时重复投递仍 204；新事件 503
# ---------------------------------------------------------------------------


def test_duplicate_post_still_204_when_queue_full(tmp_path, monkeypatch):
    app, _sender, _settings = build_app(
        tmp_path, monkeypatch, pipeline={"ordinary_queue": 1}
    )

    async def scenario():
        event = fake_event("dup-1")
        assert await post_status(app, event) == 204
        assert await post_status(app, event) == 204  # 重复投递：已接受语义
        assert await post_status(app, fake_event("dup-2")) == 503  # 新事件无容量

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #5 claim 完成映射复合键：同 message_id 不同 self_id 各自终结
# ---------------------------------------------------------------------------


def test_claims_finish_independently_per_self_id(tmp_path, monkeypatch):
    app, sender, _settings = build_app(tmp_path, monkeypatch)
    store = app.state.claim_store

    async def scenario():
        tasks = await _start_consumers(app)
        try:
            first = fake_event("same-mid", user="10001", self_id="900000001")
            second = fake_event("same-mid", user="10002", self_id="900000002")
            assert await post_status(app, first) == 204
            assert await post_status(app, second) == 204
            assert await _wait_for(lambda: len(sender.calls) >= 2)
            await app.state.processor.wait_idle()
            key1 = ("onebot", "900000001", "same-mid")
            key2 = ("onebot", "900000002", "same-mid")
            assert store.get(key1) is not None and store.get(key1).state == "done"
            assert store.get(key2) is not None and store.get(key2).state == "done"
        finally:
            await _stop_consumers(tasks)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 第 2 轮复核残留四条（P2）的回归探针
# ---------------------------------------------------------------------------


def test_ai_marker_describing_third_party_does_not_count():
    # 「DeepSeek 是一款 AI」描述的是第三方，不是自身 AI 身份披露。
    assert review_bubbles(
        ["我是阿年，DeepSeek 是一款 AI，对话经第三方模型 DeepSeek 处理。"],
        user_text="你是谁",
    ) == (False, "identity")


@pytest.mark.parametrize("question", ["你的名字？", "你的名字"])
def test_bare_name_question_triggers(question):
    assert is_identity_question(question)


def test_name_compliment_does_not_trigger():
    assert not is_identity_question("你的名字真好听")


def test_padded_ids_still_finish_claim(tmp_path, monkeypatch):
    app, sender, _settings = build_app(tmp_path, monkeypatch)
    store = app.state.claim_store

    async def scenario():
        tasks = await _start_consumers(app)
        try:
            event = fake_event(" padded-mid ", user="10001", self_id=" 900000001 ")
            assert await post_status(app, event) == 204
            assert await _wait_for(lambda: len(sender.calls) >= 1)
            await app.state.processor.wait_idle()
            record = store.get(("onebot", "900000001", "padded-mid"))
            assert record is not None and record.state == "done"
        finally:
            await _stop_consumers(tasks)

    asyncio.run(scenario())


def test_long_tool_synthesis_is_not_capped_by_multipart_limit(
    tmp_path, monkeypatch
):
    app, sender, _settings = build_app(tmp_path, monkeypatch)
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.read_only_registry.schemas = lambda: [{"type": "function"}]
    long_reply = "合规摘要" * 180  # 720 字，超过 multipart 600 字总上限

    class StubBrain:
        async def respond_with_tools(self, *_args, **_kwargs):
            return long_reply, True

    processor.brain = StubBrain()

    async def scenario():
        assert await processor.handle(private_event("10001", "帮我查一下进展"))
        assert messages(sender) == [long_reply]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #1 普通池占满时危机仍走 critical 专职池
# ---------------------------------------------------------------------------


def test_crisis_uses_dedicated_pool_while_ordinary_pool_blocked(
    tmp_path, monkeypatch
):
    app, sender, settings = build_app(
        tmp_path, monkeypatch, pipeline={"ordinary_pool": 1}
    )
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    asyncio.run(processor.conversation.mark_onboarded("10002"))
    gate = asyncio.Event()
    entered = asyncio.Event()

    class BlockingBrain:
        async def respond_multipart(self, *_args, **_kwargs):
            entered.set()
            await gate.wait()
            return '{"bubbles": ["慢回复"]}'

        async def respond(self, *_args, **_kwargs):  # pragma: no cover
            return "慢回复"

    processor.brain = BlockingBrain()

    async def scenario():
        tasks = await _start_consumers(app)
        try:
            slow = fake_event("slow-1", user="10001", text="慢慢聊")
            assert await post_status(app, slow) == 204
            assert await _wait_for(entered.is_set)  # 普通池唯一许可已被占用
            crisis = fake_event("crisis-1", user="10002", text="想自杀")
            assert await post_status(app, crisis) == 204
            expected = crisis_reply(
                settings.safety["helplines"], False, settings.safety
            )
            # 危机经专职池先行送达，普通回复仍被堵住未发。
            assert await _wait_for(lambda: expected in messages(sender))
            assert "慢回复" not in messages(sender)
            gate.set()
            assert await _wait_for(lambda: "慢回复" in messages(sender))
        finally:
            gate.set()
            await _stop_consumers(tasks)

    asyncio.run(scenario())
