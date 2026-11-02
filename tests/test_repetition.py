"""Synthetic tests for conservative repetition state and repair prompts."""

import asyncio
import json

from companion.brain import CompanionBrain
from companion.repetition import (
    SESSION_SECONDS,
    TEASE_LINE,
    RepetitionSessions,
    comparable_question,
    is_light_tone,
    requests_stop_teasing,
)
from companion.safety import crisis_reply
from tests import test_app
from tests.fake_onebot import private_event

setup_repetition_app = test_app.setup_app

LIGHT_QUESTION = "哈哈哈，你在干嘛呢？"
SERIOUS_QUESTION = "这个原理是什么？"
DISTRESS_QUESTION = "哈哈，我好难过怎么办？"


class JSONLLM:
    def __init__(self, answer="答案。"):
        self.answer = answer
        self.prompts = []

    async def generate_json(self, prompt):
        self.prompts.append(prompt)
        return json.dumps({"bubbles": [self.answer]}, ensure_ascii=False)


def run_turns(processor, texts):
    async def run():
        return [await processor.handle(private_event("10001", text)) for text in texts]

    return asyncio.run(run())


def test_matching_is_conservative_and_preserves_constraints():
    assert comparable_question("  What is 2 + 2？ ") == comparable_question("what is 2 + 2?")
    assert comparable_question("What is 2 + 2?") != comparable_question("What is 2 + 3?")
    assert comparable_question("Is it safe?") != comparable_question("Is it not safe?")
    assert comparable_question("What's the weather?") is None
    assert comparable_question("不要调侃好吗？") is None
    assert comparable_question("hello") is None


def test_session_acceptance_shift_expiry_and_reset():
    now = [0.0]
    sessions = RepetitionSessions(clock=lambda: now[0])

    def begin(text, accepted=True):
        level, token = sessions.begin("synthetic", text)
        sessions.finish("synthetic", token, accepted)
        return level

    assert begin("How does this work?") == 0
    assert begin("How does this work?") == 1
    assert begin("How does this work?", False) == 2
    assert begin("How does this work?") == 0
    assert begin("Another question?") == 0
    assert begin("How does this work?") == 0
    now[0] += SESSION_SECONDS
    assert begin("How does this work?") == 0
    sessions.reset("synthetic")
    assert begin("How does this work?") == 0
    sessions.close()
    assert not sessions.sessions


def test_repair_prompts_follow_accepted_answers_and_forget(setup_repetition_app):
    app, _, _, settings = setup_repetition_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    prompts = []

    class LLM:
        async def generate(self, prompt):
            prompts.append(prompt)
            return "Synthetic answer."

        async def generate_json(self, prompt):
            prompts.append(prompt)
            return '{"bubbles": ["Synthetic answer."]}'

    processor.brain = CompanionBrain(settings.persona, LLM())

    async def run():
        event = private_event("10001", "How does this work?")
        assert await processor.handle(event)
        assert await processor.handle(event)
        assert "之前的解释可能没有说到点上" in prompts[-1]
        assert await processor.handle(event)
        assert "简短而有用地重述或换个例子" in prompts[-1]
        await processor.handle(private_event("10001", "忘掉我"))
        assert await processor.handle(event)
        assert "这可能是用户重复的问题" not in prompts[-1]

    asyncio.run(run())


def test_authenticated_duplicate_does_not_advance_repeat(setup_repetition_app):
    import json

    import httpx

    app, _, _, settings = setup_repetition_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    prompts = []

    class LLM:
        async def generate(self, prompt):
            prompts.append(prompt)
            return "Synthetic answer."

        async def generate_json(self, prompt):
            prompts.append(prompt)
            return '{"bubbles": ["Synthetic answer."]}'

    processor.brain = CompanionBrain(settings.persona, LLM())

    async def run():
        worker = asyncio.create_task(app.state.event_consumer())
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
            ) as client:
                event = private_event("10001", "How does this work?")
                event["message_id"] = "synthetic-1"
                body = json.dumps(event).encode()
                headers = {"X-Signature": test_app.signed(body)}
                response = await client.post("/onebot/event", content=body, headers=headers)
                assert response.status_code == 204
                await app.state.event_bus.join()
                await app.state.processor.wait_idle()
                response = await client.post("/onebot/event", content=body, headers=headers)
                assert response.status_code == 204
                await app.state.event_bus.join()
                await app.state.processor.wait_idle()
                assert len(prompts) == 1
                event["message_id"] = "synthetic-2"
                body = json.dumps(event).encode()
                await client.post("/onebot/event", content=body,
                                  headers={"X-Signature": test_app.signed(body)})
                await app.state.event_bus.join()
                await app.state.processor.wait_idle()
                assert len(prompts) == 2
                assert "之前的解释可能没有说到点上" in prompts[-1]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            processor.repetition.close()

    asyncio.run(run())


def test_tease_helpers_are_conservative():
    assert requests_stop_teasing("别调侃我啦")
    assert requests_stop_teasing("Stop teasing me")
    assert not requests_stop_teasing("哈哈哈")
    assert is_light_tone(LIGHT_QUESTION, [])
    assert not is_light_tone(SERIOUS_QUESTION, [])
    assert not is_light_tone(DISTRESS_QUESTION, [])
    assert not is_light_tone("哈哈哈", [("user", "我好难过")])


def test_light_repeat_adds_one_fixed_gentle_tease_bubble(setup_repetition_app):
    app, calls, _, settings = setup_repetition_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    llm = JSONLLM()
    processor.brain = CompanionBrain(settings.persona, llm)

    assert all(run_turns(processor, [LIGHT_QUESTION, LIGHT_QUESTION, LIGHT_QUESTION]))
    messages = [call["message"] for call in calls]
    assert messages == ["答案。", "答案。", "答案。", TEASE_LINE]
    assert "之前的解释可能没有说到点上" in llm.prompts[1]
    assert "简短而有用地重述或换个例子" in llm.prompts[2]
    assert TEASE_LINE not in llm.prompts[2]  # the fixed line is never model-generated


def test_repeat_without_clear_light_tone_never_teases_or_refuses(setup_repetition_app):
    app, calls, _, settings = setup_repetition_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.brain = CompanionBrain(settings.persona, JSONLLM())

    assert all(run_turns(processor, [SERIOUS_QUESTION] * 3))
    messages = [call["message"] for call in calls]
    assert messages == ["答案。"] * 3
    assert TEASE_LINE not in messages


def test_distress_with_banter_markers_omits_teasing(setup_repetition_app):
    app, calls, _, settings = setup_repetition_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    processor.brain = CompanionBrain(settings.persona, JSONLLM())

    assert all(run_turns(processor, [DISTRESS_QUESTION] * 3))
    assert TEASE_LINE not in [call["message"] for call in calls]


def test_stop_teasing_request_wins_immediately_for_the_session(setup_repetition_app):
    app, calls, _, settings = setup_repetition_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    llm = JSONLLM()
    processor.brain = CompanionBrain(settings.persona, llm)

    texts = [LIGHT_QUESTION, LIGHT_QUESTION, "别调侃我啦", LIGHT_QUESTION] * 1
    texts += [LIGHT_QUESTION, LIGHT_QUESTION]
    assert all(run_turns(processor, texts))
    messages = [call["message"] for call in calls]
    assert TEASE_LINE not in messages
    # The last two repeats reach the higher ladder but remain unt teased.
    assert "简短而有用地重述或换个例子" in llm.prompts[-1]


def test_tool_routed_turn_resets_repeat_state_conservatively(setup_repetition_app):
    app, calls, _, settings = setup_repetition_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50

    class Registry:
        def __init__(self):
            self.enabled = False

        def schemas(self):
            return [{"type": "function", "function": {"name": "lookup"}}] if self.enabled else []

        def is_enabled(self, _name):
            return False

    class Brain:
        def __init__(self):
            self.prompts = []
            self.tool_calls = 0

        async def respond_multipart(self, message, history, *, persona=None, repeat_level=0):
            self.prompts.append((message, repeat_level))
            return json.dumps({"bubbles": [f"回答：{message}"]}, ensure_ascii=False)

        async def respond_with_tools(self, *_args, **_kwargs):
            self.tool_calls += 1
            return "工具回答。", True

    brain = Brain()
    registry = Registry()
    processor.read_only_registry = registry
    processor.brain = brain
    assert all(run_turns(processor, [LIGHT_QUESTION, LIGHT_QUESTION]))
    registry.enabled = True
    assert asyncio.run(processor.handle(private_event("10001", "查一下这个")))
    registry.enabled = False
    assert asyncio.run(processor.handle(private_event("10001", LIGHT_QUESTION)))
    assert brain.tool_calls == 1
    assert brain.prompts[-1] == (LIGHT_QUESTION, 0)
    assert "工具回答。" in [call["message"] for call in calls]
    assert TEASE_LINE not in [call["message"] for call in calls]


def test_crisis_precedes_repetition_and_is_never_suppressed(setup_repetition_app):
    app, calls, _, settings = setup_repetition_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.limiter.limit = 50
    llm = JSONLLM()
    processor.brain = CompanionBrain(settings.persona, llm)

    assert all(run_turns(processor, [LIGHT_QUESTION, LIGHT_QUESTION]))
    assert asyncio.run(processor.handle(private_event("10001", "想自杀")))
    expected = crisis_reply(settings.safety["helplines"], False, settings.safety)
    assert calls[-1]["message"] == expected
    assert asyncio.run(processor.handle(private_event("10001", LIGHT_QUESTION)))
    assert "之前的解释可能没有说到点上" not in llm.prompts[-1]
    assert TEASE_LINE not in [call["message"] for call in calls]
