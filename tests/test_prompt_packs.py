"""Synthetic tests for reviewed pack selection and fixed safety routing."""

import asyncio
import hashlib
import json

import pytest

from companion.brain import CompanionBrain
from companion.prompt_packs import REVISION, SOURCE, PackRegistry
from companion.safety import crisis_reply
from tests import test_app
from tests.fake_onebot import private_event

setup_packs_app = test_app.setup_app


def make_registry(path):
    content = b'Synthetic voice only.'
    (path / 'friend.txt').write_bytes(content)
    entry = {
        'id': 'friend', 'title': 'Synthetic entry', 'style': '示例风格', 'preview': '示例回复。',
        'source_repository': SOURCE, 'revision': REVISION, 'license': 'CC0-1.0',
        'file': 'friend.txt', 'sha256': hashlib.sha256(content).hexdigest(),
        'integrity_note': 'Synthetic fixture', 'contributor': 'synthetic', 'approved': True,
    }
    registry = {'version': 1, 'default': 'default', 'packs': [entry]}
    (path / 'registry.json').write_text(json.dumps(registry), encoding='utf-8')
    return registry


def test_registry_validates_integrity_approval_and_duplicates(tmp_path):
    registry = make_registry(tmp_path)
    assert PackRegistry(tmp_path).resolve('friend').text == 'Synthetic voice only.'
    (tmp_path / 'friend.txt').write_text('modified', encoding='utf-8')
    assert PackRegistry(tmp_path).resolve('friend').id == 'default'
    make_registry(tmp_path)
    registry['packs'][0]['approved'] = False
    (tmp_path / 'registry.json').write_text(json.dumps(registry), encoding='utf-8')
    assert PackRegistry(tmp_path).resolve('friend').id == 'default'
    registry['packs'][0]['approved'] = True
    registry['packs'].append(registry['packs'][0])
    (tmp_path / 'registry.json').write_text(json.dumps(registry), encoding='utf-8')
    assert PackRegistry(tmp_path).resolve('friend').id == 'default'
    assert PackRegistry(tmp_path / 'missing').resolve('friend').id == 'default'


def test_selection_fallback_forget_and_fixed_routes(setup_packs_app, tmp_path):
    app, calls, _, settings = setup_packs_app
    processor = app.state.processor
    make_registry(tmp_path)
    processor.pack_registry = PackRegistry(tmp_path)
    processor.limiter.limit = 50
    prompts = []

    class FakeLLM:
        async def generate(self, prompt):
            prompts.append(prompt)
            return 'synthetic response'

    processor.brain = CompanionBrain(settings.persona, FakeLLM())

    async def send(user, text):
        return await processor.handle(private_event(user, text))

    async def run():
        (await processor.conversation.mark_onboarded('10001'))
        (await processor.conversation.mark_onboarded('10002'))
        await send('10001', '聊天风格')
        assert '示例回复。' in calls[-1]['message']
        assert (await processor.conversation.pack_snapshot('10001'))[0] is None
        await send('10001', '切换风格 friend')
        await send('10001', '你是谁')
        assert 'Synthetic voice only.' in prompts[-1]
        assert '第三方模型处理' in prompts[-1] and 'AI 陪伴助手' in prompts[-1]
        await send('10002', '你好')
        assert prompts[-1].startswith(settings.persona)
        before = len(prompts)
        await send('10001', '想自杀')
        assert calls[-1]['message'] == crisis_reply(settings.safety['helplines'], False,
                                                    settings.safety)
        assert len(prompts) == before
        await send('10001', '恢复默认风格')
        assert (await processor.conversation.pack_snapshot('10001'))[0] is None
        await send('10001', '切换风格 friend')
        await send('10001', '切换风格 unknown')
        assert (await processor.conversation.pack_snapshot('10001'))[0] is None
        (await processor.conversation.select_pack('10001', 'removed'))
        await send('10001', '你好')
        assert prompts[-1].startswith(settings.persona)
        await send('10001', '切换风格 friend')
        await send('10001', '忘掉我')
        assert (await processor.conversation.pack_snapshot('10001'))[0] is None
        assert not (await processor.conversation.get_history('10001'))

    asyncio.run(run())


def test_identity_disclosure_is_enforced_under_a_switched_pack(setup_packs_app, tmp_path):
    app, calls, _, settings = setup_packs_app
    processor = app.state.processor
    make_registry(tmp_path)
    processor.pack_registry = PackRegistry(tmp_path)
    asyncio.run(processor.conversation.mark_onboarded('10001'))
    processor.limiter.limit = 50
    assert asyncio.run(processor.handle(private_event('10001', '切换风格 friend')))
    prompts = []

    class FakeLLM:
        def __init__(self, answer):
            self.answer = answer

        async def generate_json(self, prompt):
            prompts.append(prompt)
            return json.dumps({'bubbles': [self.answer]}, ensure_ascii=False)

    full_disclosure = '我是阿年，一个 AI 陪伴助手。当前回复由第三方模型 DeepSeek 生成。'
    processor.brain = CompanionBrain(settings.persona, FakeLLM(full_disclosure))
    assert asyncio.run(processor.handle(private_event('10001', '你是谁')))
    assert calls[-1]['message'] == full_disclosure
    assert 'Synthetic voice only.' in prompts[-1]
    assert '第三方模型处理' in prompts[-1] and 'AI 陪伴助手' in prompts[-1]

    processor.brain = CompanionBrain(settings.persona, FakeLLM('我是真人，不是 AI。'))
    assert asyncio.run(processor.handle(private_event('10001', '你是真人吗')))
    assert calls[-1]['message'] == processor.identity_fallback
    assert '我是真人' not in calls[-1]['message']
    assert '第三方模型' in calls[-1]['message']


@pytest.mark.parametrize('command', ['恢复默认风格', '忘掉我'])
def test_inflight_response_cannot_restore_old_state(setup_packs_app, tmp_path, command):
    app, calls, _, settings = setup_packs_app
    processor = app.state.processor
    make_registry(tmp_path)
    processor.pack_registry = PackRegistry(tmp_path)
    asyncio.run(processor.conversation.mark_onboarded('10001'))
    asyncio.run(processor.conversation.select_pack('10001', 'friend'))

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingLLM:
            async def generate(self, prompt):
                entered.set()
                await release.wait()
                return 'old response'

        processor.brain = CompanionBrain(settings.persona, BlockingLLM())
        pending = asyncio.create_task(processor.handle(private_event('10001', '你好')))
        await entered.wait()
        await processor.handle(private_event('10001', command))
        release.set()
        assert not await pending
        assert (await processor.conversation.pack_snapshot('10001'))[0] is None
        assert not (await processor.conversation.get_history('10001'))
        assert all(call['message'] != 'old response' for call in calls)

    asyncio.run(run())
