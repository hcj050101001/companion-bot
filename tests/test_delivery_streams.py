"""Synthetic delivery-stream tests: pacing, cancellation, priority, and state."""

import asyncio
import json
import sqlite3

import pytest

from companion.brain import CompanionBrain
from companion.ingress import MAX_TOTAL_PACING_SECONDS
from companion.safety import crisis_reply
from tests import test_app
from tests.fake_onebot import private_event

setup_stream_app = test_app.setup_app


def raw(*bubbles):
    return json.dumps({"bubbles": list(bubbles)}, ensure_ascii=False)


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []

    async def generate_json(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        value = self.responses.pop(0)
        if callable(value):
            return await value()
        return value


class RecordingSender:
    def __init__(self):
        self.calls = []
        self.block_indices = set()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.failures = {}

    async def send_private_msg(self, user_id, message, *, priority="normal"):
        index = len(self.calls)
        self.calls.append({"user_id": int(user_id), "message": message, "priority": priority})
        if index in self.block_indices:
            self.entered.set()
            await self.release.wait()
        failure = self.failures.get(index)
        if failure is None:
            return {"status": "ok"}
        if isinstance(failure, Exception):
            raise failure
        return {"status": failure}


class Pacing:
    def __init__(self, block_index=None):
        self.delays = []
        self.block_index = block_index
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def sleep(self, delay):
        index = len(self.delays)
        self.delays.append(delay)
        if index == self.block_index:
            self.entered.set()
            await self.release.wait()


async def wait_until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.001)


@pytest.fixture
def stream_processor(setup_stream_app):
    _, _, _, settings = setup_stream_app
    processor = setup_stream_app[0].state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 100
    sender = RecordingSender()
    processor.sender = sender
    return processor, sender, settings


def test_paces_three_bubbles_and_stores_only_accepted_text(stream_processor):
    processor, sender, settings = stream_processor
    bubbles = ["先回应你。", "再说第一件事。", "最后提醒一句。"]
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw(*bubbles)]))
    pacing = Pacing()
    processor.sleep = pacing.sleep
    ranges = []

    def random_uniform(low, high):
        ranges.append((low, high))
        return 0.0 if high == 0 else 1.0

    processor.random_uniform = random_uniform
    assert asyncio.run(processor.handle(private_event("10001", "synthetic question")))
    assert [call["message"] for call in sender.calls] == bubbles
    assert ranges == [(0.0, 0.0), (0.8, 1.5), (0.8, 1.5)]
    assert pacing.delays == [0.0, 1.0, 1.0]
    assert asyncio.run(processor.conversation.get_history("10001")) == [
        ("user", "synthetic question"),
        ("assistant", "\n".join(bubbles)),
    ]


def test_total_pacing_cap_bounds_all_waits(stream_processor):
    processor, sender, settings = stream_processor
    bubbles = ["第一条。", "第二条。", "第三条。"]
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw(*bubbles)]))
    pacing = Pacing()
    processor.sleep = pacing.sleep
    processor.random_uniform = lambda _low, high: 6.0 if high > 0 else 0.0

    assert asyncio.run(processor.handle(private_event("10001", "synthetic question")))
    assert len(sender.calls) == 3
    assert sum(pacing.delays) == pytest.approx(MAX_TOTAL_PACING_SECONDS)


def test_crisis_during_pacing_cancels_unsent_bubbles(stream_processor):
    processor, sender, settings = stream_processor
    bubbles = ["第一条。", "第二条。", "第三条。"]
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw(*bubbles)]))
    pacing = Pacing(block_index=1)
    processor.sleep = pacing.sleep
    processor.random_uniform = lambda _low, high: 0.0 if high == 0 else 1.0
    expected = crisis_reply(settings.safety["helplines"], False, settings.safety)

    async def run():
        ordinary = asyncio.create_task(
            processor.handle(private_event("10001", "synthetic question"))
        )
        await asyncio.wait_for(pacing.entered.wait(), timeout=1)
        assert [call["message"] for call in sender.calls] == [bubbles[0]]
        crisis = asyncio.create_task(processor.handle(private_event("10001", "想自杀")))
        await wait_until(
            lambda: any(call["message"] == expected for call in sender.calls)
        )
        assert sender.calls[-1]["priority"] == "critical"
        assert len(sender.calls) == 2
        pacing.release.set()
        assert not await ordinary
        assert await crisis
        assert [call["message"] for call in sender.calls] == [bubbles[0], expected]

    asyncio.run(run())


def test_crisis_does_not_wait_for_an_inflight_ordinary_http_request(stream_processor):
    processor, sender, settings = stream_processor
    bubbles = ["唯一的一条。"]
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw(*bubbles)]))
    processor.random_uniform = lambda _low, _high: 0.0
    sender.block_indices.add(0)
    expected = crisis_reply(settings.safety["helplines"], False, settings.safety)

    async def run():
        ordinary = asyncio.create_task(
            processor.handle(private_event("10001", "synthetic question"))
        )
        await asyncio.wait_for(sender.entered.wait(), timeout=1)
        assert sender.calls[0]["message"] == bubbles[0]
        crisis = asyncio.create_task(processor.handle(private_event("10001", "想自杀")))
        await wait_until(lambda: len(sender.calls) >= 2)
        assert sender.calls[-1]["message"] == expected
        assert sender.calls[-1]["priority"] == "critical"
        sender.release.set()
        assert not await ordinary
        assert await crisis
        # At most the single old in-flight bubble arrived; nothing else followed it.
        assert [call["message"] for call in sender.calls] == [bubbles[0], expected]

    asyncio.run(run())


def test_interruption_during_generation_discards_the_old_answer(stream_processor):
    processor, sender, settings = stream_processor
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingLLM:
        def __init__(self):
            self.first = True

        async def generate_json(self, _prompt):
            if self.first:
                self.first = False
                entered.set()
                await release.wait()
                return raw("old answer")
            return raw("new answer")

    processor.brain = CompanionBrain(settings.persona, BlockingLLM())

    async def run():
        old = asyncio.create_task(
            processor.handle(private_event("10001", "old question"))
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        new = asyncio.create_task(
            processor.handle(private_event("10001", "new question"))
        )
        release.set()
        assert not await old
        assert await new

    asyncio.run(run())
    assert [call["message"] for call in sender.calls] == ["new answer"]


def test_interruption_during_pacing_discards_the_old_answer(stream_processor):
    processor, sender, settings = stream_processor
    bubbles = ["旧的第一条。", "旧的第二条。", "旧的第三条。"]
    processor.brain = CompanionBrain(
        settings.persona, ScriptedLLM([raw(*bubbles), raw("新的回答。")])
    )
    pacing = Pacing(block_index=1)
    processor.sleep = pacing.sleep
    processor.random_uniform = lambda _low, high: 0.0 if high == 0 else 1.0

    async def run():
        old = asyncio.create_task(
            processor.handle(private_event("10001", "old question"))
        )
        await asyncio.wait_for(pacing.entered.wait(), timeout=1)
        new = asyncio.create_task(
            processor.handle(private_event("10001", "new question"))
        )
        assert await new
        assert not await old

    asyncio.run(run())
    assert [call["message"] for call in sender.calls] == [bubbles[0], "新的回答。"]


def test_forget_during_a_stream_wins_immediately(stream_processor):
    processor, sender, settings = stream_processor
    bubbles = ["第一条。", "第二条。", "第三条。"]
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw(*bubbles)]))
    pacing = Pacing(block_index=1)
    processor.sleep = pacing.sleep
    processor.random_uniform = lambda _low, high: 0.0 if high == 0 else 1.0

    async def run():
        ordinary = asyncio.create_task(
            processor.handle(private_event("10001", "synthetic question"))
        )
        await asyncio.wait_for(pacing.entered.wait(), timeout=1)
        forget = asyncio.create_task(processor.handle(private_event("10001", "忘掉我")))
        await wait_until(
            lambda: any("已删除" in call["message"] for call in sender.calls)
        )
        pacing.release.set()
        assert not await ordinary
        assert await forget

    asyncio.run(run())
    assert [call["message"] for call in sender.calls] == [
        bubbles[0], "好的，我已删除保存的短期记忆。"
    ]
    assert asyncio.run(processor.conversation.get_history("10001")) == []


def test_duplicate_message_id_is_not_delivered_twice(stream_processor):
    processor, sender, settings = stream_processor
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw("回答。"), raw("回答。")]))
    event = private_event("10001", "question")
    event["message_id"] = "synthetic-duplicate"

    assert asyncio.run(processor.handle(event))
    assert not asyncio.run(processor.handle(event))
    assert [call["message"] for call in sender.calls] == ["回答。"]


def test_partial_then_ambiguous_send_stores_only_the_accepted_prefix(stream_processor):
    processor, sender, settings = stream_processor
    bubbles = ["已接受的第一条。", "发送失败的第二条。", "从未发送的第三条。"]
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw(*bubbles)]))
    processor.random_uniform = lambda _low, _high: 0.0
    sender.failures[1] = OSError("synthetic timeout")

    assert not asyncio.run(processor.handle(private_event("10001", "question")))
    assert [call["message"] for call in sender.calls] == bubbles[:2]
    # P5 台账落点为 delivery_turns（messages 只存历史投影）：断言语义
    # 不变 — 状态 ambiguous 且只存已接受的第一条。
    with sqlite3.connect(processor.database.path) as db:
        rows = db.execute(
            "SELECT state, accepted_text FROM delivery_turns WHERE user_id=?",
            ("10001",),
        ).fetchall()
    assert rows == [("ambiguous", bubbles[0])]
    assert asyncio.run(processor.conversation.get_history("10001")) == [("user", "question")]


def test_blocked_before_http_is_recorded_as_failed(stream_processor):
    processor, sender, settings = stream_processor
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw("回答。")]))
    processor.random_uniform = lambda _low, _high: 0.0
    sender.failures[0] = "rate_limited"

    assert not asyncio.run(processor.handle(private_event("10001", "question")))
    # 同上：台账状态 failed 且未存任何已接受文本。
    with sqlite3.connect(processor.database.path) as db:
        rows = db.execute(
            "SELECT state, accepted_text FROM delivery_turns WHERE user_id=?",
            ("10001",),
        ).fetchall()
    assert rows == [("failed", "")]
    assert asyncio.run(processor.conversation.get_history("10001")) == [("user", "question")]


def test_bounded_stream_capacity_fails_closed(stream_processor):
    processor, sender, settings = stream_processor
    llm = ScriptedLLM([raw("回答。")])
    processor.brain = CompanionBrain(settings.persona, llm)

    async def run():
        processor._stream_capacity = asyncio.Semaphore(0)
        processor._stream_wait_seconds = 0.01
        assert not await processor.handle(private_event("10001", "question"))

    asyncio.run(run())
    assert llm.calls == 0
    assert sender.calls == []
    records = [json.loads(line) for line in processor.log_path.read_text().splitlines()]
    assert records[-1]["event_type"] == "rate_limited"


def test_new_event_waits_for_inflight_accepted_record_before_advancing(stream_processor):
    """E 路修复 2（Codex 时序复现）：气泡 accepted → 记账写未落 →
    新事件到达。代次推进必须等未结记账写落定，否则台账围栏拒写、
    台账停 in_flight、历史缺该回复。修复后最终台账 complete、
    accepted_text 在、历史含该回复，且新事件照常处理。
    """
    processor, sender, settings = stream_processor
    processor.brain = CompanionBrain(
        settings.persona, ScriptedLLM([raw("先回应你。"), raw("新的回答。")])
    )
    processor.random_uniform = lambda _low, _high: 0.0

    ledger = processor.ledger
    original_update = ledger.update_delivery
    record_entered = asyncio.Event()
    record_release = asyncio.Event()
    blocked = False

    async def gated_update(*args, **kwargs):
        nonlocal blocked
        if not blocked and kwargs.get("accepted_bubble") is not None:
            blocked = True
            record_entered.set()
            await record_release.wait()
        return await original_update(*args, **kwargs)

    ledger.update_delivery = gated_update

    async def scenario():
        old_event = private_event("10001", "synthetic question")
        old_event["message_id"] = "race-old"
        old = asyncio.create_task(processor.handle(old_event))
        await asyncio.wait_for(record_entered.wait(), timeout=5)
        generation_before = await ledger.delivery_generation("10001")
        new_event = private_event("10001", "new question")
        new_event["message_id"] = "race-new"
        dispatching = asyncio.create_task(processor.dispatch(new_event))
        await asyncio.sleep(0.1)
        # 记账写未落之前，新事件不得推进代次（推进先行正是竞态根源）。
        assert not dispatching.done()
        assert await ledger.delivery_generation("10001") == generation_before
        record_release.set()
        new_task = await asyncio.wait_for(dispatching, timeout=5)
        assert new_task is not None
        assert await asyncio.wait_for(new_task, timeout=5)
        assert await asyncio.wait_for(old, timeout=5)

    asyncio.run(scenario())
    assert [call["message"] for call in sender.calls] == ["先回应你。", "新的回答。"]
    with sqlite3.connect(processor.database.path) as db:
        rows = db.execute(
            "SELECT turn_id, state, accepted_text FROM delivery_turns "
            "WHERE user_id=? ORDER BY rowid",
            ("10001",),
        ).fetchall()
    assert ("race-old", "complete", "先回应你。") in rows
    history = asyncio.run(processor.conversation.get_history("10001"))
    assert ("assistant", "先回应你。") in history
    assert ("assistant", "新的回答。") in history


def test_late_delivery_write_after_forget_is_still_rejected(stream_processor):
    """E 路修复 2（围栏保留）：forget 落定后，迟到的旧代次记账写仍须
    被台账删除围栏拒绝，绝不借串行通道复活已删回复。
    """
    processor, sender, settings = stream_processor
    processor.brain = CompanionBrain(
        settings.persona, ScriptedLLM([raw("先回应你。")])
    )
    processor.random_uniform = lambda _low, _high: 0.0

    async def scenario():
        event = private_event("10001", "synthetic question")
        event["message_id"] = "forget-late"
        assert await processor.handle(event)
        generation = await processor.ledger.delivery_generation("10001")
        # 忘掉：清历史+台账并推进代次（删除围栏）。
        await processor.conversation.forget("10001")
        # 迟到写：以忘掉前的代次补写 accepted，必须被拒。
        accepted = await processor.ledger.update_delivery(
            "10001", "forget-late", "complete", 0, 0, generation,
            accepted_bubble="先回应你。",
        )
        assert accepted is False
        assert await processor.conversation.get_history("10001") == []

    asyncio.run(scenario())


def test_cancelled_delivery_keeps_sent_bubble_in_history(stream_processor):
    """P5 Review #4 回归：气泡已送达、其台账记录写在途时投递被取消，
    记录写必须先落盘——已送达气泡不得从历史中消失。"""
    processor, sender, settings = stream_processor
    bubbles = ["先回应你。", "再说第二件事。"]
    processor.brain = CompanionBrain(settings.persona, ScriptedLLM([raw(*bubbles)]))
    processor.sleep = Pacing().sleep
    processor.random_uniform = lambda _low, _high: 0.0

    ledger = processor.ledger
    original_update = ledger.update_delivery
    record_entered = asyncio.Event()
    record_release = asyncio.Event()
    blocked = False

    async def gated_update(*args, **kwargs):
        nonlocal blocked
        if not blocked and kwargs.get("accepted_bubble") is not None:
            blocked = True
            record_entered.set()
            await record_release.wait()
        return await original_update(*args, **kwargs)

    ledger.update_delivery = gated_update

    async def scenario():
        task = asyncio.create_task(
            processor.handle(private_event("10001", "synthetic question"))
        )
        await asyncio.wait_for(record_entered.wait(), timeout=5)
        task.cancel()
        record_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [call["message"] for call in sender.calls] == bubbles[:1]
    history = asyncio.run(processor.conversation.get_history("10001"))
    assert ("assistant", "先回应你。") in history
