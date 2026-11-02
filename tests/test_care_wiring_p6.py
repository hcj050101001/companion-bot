"""P6 关怀接线集成测试：Admission 关怀控制命令 + 调度发送门的端到端验收。

对应 P6 验收条目（方案 §5.3/§5.8、KD-S03）：

1. 关怀控制命令流（开启/暂停/恢复/停止/关闭 + 无同意口径）：名单内用户
   的精确命令在 Admission 前置分支处理，只回确认文案、不走 brain。
2. 非名单用户无命令面：同文本走普通链，且不落任何关怀同意。
3. 未接线等价：care_preferences 为 None 时行为与接线前基线一致。
4. KD-S03 关闭：「忘掉」在同一临界区内连带删除关怀同意并失效待发关怀，
   确认文案与会话记忆清除口径不变。
5. 撤回保底：judge 故障（D1 疑似危机路径）+ 入站限流耗尽时，「停止
   关怀提醒」仍先落库删除并送达确认——撤回权不因下游故障落空。
6. 排队→撤回→触发→零发送：真 CareSendGate + CareScheduler 下，撤回使
   待发项失效，run_due 实际发送次数为 0、无 REMINDER 落到发送端。
7. 主动关怀总开关 enabled=false 时门恒关零发送，但同意本身保留。
8. Stage 0 预筛分道：四个关怀命令经 classify_lane 判 control，普通
   文本判 ordinary。
9. 发送中撤回不重试（降级集成版）：假 gate 发送阻塞期间 invalidate，
   释放后该项不被再次发送（gate.send 总调用次数恒为 1）。

夹具口径：复用 tests.test_app.setup_app（USER="10001" 在 allowed 内、
FakeSender 记 calls、judge 已换成回 NO 的 SafeJudgeLLM）；关怀偏好库
建在仓库外 tmp_path；processor 侧时间经 care_clock 注入假时钟 START
（UTC 01:00 = 上海 09:00，非静默时段），全程不依赖真实时间流逝。
"""

import asyncio
from datetime import datetime, timezone

import pytest

from companion.domain.claims import classify_lane
from companion.ingress import RateLimiter
from companion.memory.care import CarePreferences
from companion.safety import ModelLLMJudge
from companion.safety.care import REMINDER, CareSendGate
from companion.scheduler import CareScheduler
from tests import test_app
from tests.fake_onebot import private_event

setup_app = test_app.setup_app

USER = "10001"  # setup_app 名单内用户
STRANGER = "20002"  # 名单外用户
START = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)  # 上海 09:00

OPT_IN_CMD = "开启关怀提醒"
PAUSE_CMD = "暂停关怀提醒"
STOP_CMD = "停止关怀提醒"
CLOSE_CMD = "关闭关怀提醒"

OPT_IN_REPLY = "好的，已为你开启关怀提醒，30 天内有效。想暂停或停止，随时告诉我。"
PAUSE_REPLY = "好的，关怀提醒已暂停。"
STOP_REPLY = "好的，已停止关怀提醒，你的关怀设置已删除。"
NO_CONSENT_REPLY = "你还没有开启关怀提醒。"
FORGET_REPLY = "好的，我已删除保存的短期记忆。"
NORMAL_REPLY = "测试回复"


def messages(calls, user=None):
    """calls 中（可按用户过滤）的出站文案序列。"""
    return [
        call["message"]
        for call in calls
        if user is None or call["user_id"] == int(user)
    ]


def wire_care(app, tmp_path, state):
    """属性赋值接线（规格口径）：偏好存储 + 假时钟注入 processor。"""
    processor = app.state.processor
    store = CarePreferences(tmp_path / "care.db")
    processor.care_preferences = store
    processor.care_clock = lambda: state["now"]
    return processor, store


def build_gate_scheduler(processor, store, state, *, enabled):
    """真 CareSendGate（共享 processor.delivery 额度）+ CareScheduler。"""
    gate = CareSendGate(
        store,
        processor.delivery,
        whitelist=processor.care_whitelist,
        current_safety=processor.current_care_safety,
        config={"proactive_care": {"enabled": enabled}},
        clock=lambda: state["now"],
    )
    scheduler = CareScheduler(gate, clock=lambda: state["now"])
    processor.care_gate = gate
    processor.care_scheduler = scheduler
    return gate, scheduler


# ---------------------------------------------------------------------------
# 1. 命令流：opt-in / pause / 恢复 / stop / 无同意口径
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_care_command_flow(setup_app, tmp_path):
    app, calls, _, _ = setup_app
    state = {"now": START}

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)

        assert await processor.handle(private_event(USER, OPT_IN_CMD))
        assert store.consent_state(USER, now=START) == "active"
        # 只有确认文案一条：不走 brain 的「测试回复」，且无 onboarding 干扰。
        assert messages(calls) == [OPT_IN_REPLY]

        assert await processor.handle(private_event(USER, PAUSE_CMD))
        assert store.consent_state(USER, now=START) == "paused"
        assert messages(calls) == [OPT_IN_REPLY, PAUSE_REPLY]

        assert await processor.handle(private_event(USER, OPT_IN_CMD))
        assert store.consent_state(USER, now=START) == "active"

        assert await processor.handle(private_event(USER, STOP_CMD))
        assert store.consent_state(USER, now=START) == "none"
        assert messages(calls)[-1] == STOP_REPLY

        # 无同意时 stop / pause / close 一律回未开启口径，不落任何记录。
        for command in (STOP_CMD, PAUSE_CMD, CLOSE_CMD):
            assert await processor.handle(private_event(USER, command))
            assert messages(calls)[-1] == NO_CONSENT_REPLY
            assert store.consent_state(USER, now=START) == "none"

        assert NORMAL_REPLY not in messages(calls)
        assert all(call["user_id"] == int(USER) for call in calls)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2. 非名单用户：无命令面，走普通链且不落同意
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_care_command_from_non_whitelisted_user_is_ordinary(setup_app, tmp_path):
    app, calls, _, _ = setup_app
    state = {"now": START}

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(STRANGER)

        assert await processor.handle(private_event(STRANGER, OPT_IN_CMD))
        assert store.consent_state(STRANGER, now=START) == "none"
        assert messages(calls, STRANGER) == [NORMAL_REPLY]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3. 未接线等价：care_preferences 为 None 时走普通链（基线行为）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_care_command_without_wiring_matches_baseline(setup_app):
    app, calls, _, _ = setup_app
    processor = app.state.processor
    assert processor.care_preferences is None

    async def scenario():
        await processor.conversation.mark_onboarded(USER)
        assert await processor.handle(private_event(USER, OPT_IN_CMD))
        assert messages(calls) == [NORMAL_REPLY]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. KD-S03：「忘掉」连带清关怀同意 + 失效待发，确认文案不变
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_forget_covers_care_consent_and_pending(setup_app, tmp_path):
    app, calls, _, _ = setup_app
    state = {"now": START}

    class RecordingGate:
        """待发失效断言只需调度器记账，本 gate 永不被触发发送。"""

        def __init__(self):
            self.sent = []

        async def send(self, user_id, work_id, created_at):
            self.sent.append((user_id, work_id))
            return True

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)
        scheduler = CareScheduler(RecordingGate(), clock=lambda: state["now"])
        processor.care_scheduler = scheduler

        assert await processor.handle(private_event(USER, OPT_IN_CMD))
        assert store.consent_state(USER, now=START) == "active"
        await processor.conversation.add_turn(USER, "合成提问", "合成回答")
        assert await processor.conversation.get_history(USER)
        assert scheduler.enqueue(USER, "p6-forget-1", START, due_at=START)
        assert scheduler.pending_count(USER) == 1

        assert await processor.handle(private_event(USER, "忘掉"))
        assert messages(calls) == [OPT_IN_REPLY, FORGET_REPLY]
        assert not await processor.conversation.get_history(USER)
        assert store.consent_state(USER, now=START) == "none"
        assert scheduler.pending_count(USER) == 0
        assert scheduler.pending_count() == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5. 撤回保底：judge 故障 + 限流耗尽时停止命令仍落库并送达确认
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_stop_survives_judge_failure_and_exhausted_limiter(setup_app, tmp_path):
    app, calls, _, _ = setup_app
    state = {"now": START}

    class BrokenJudgeClient:
        async def generate(self, _prompt):
            raise RuntimeError("synthetic judge outage")

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)
        assert await processor.handle(private_event(USER, OPT_IN_CMD))
        assert store.consent_state(USER, now=START) == "active"

        healthy_judge = processor.safety.judge
        # judge 故障：走 ModelLLMJudge 的 generate 路径抛错，SafetyGateway
        # 按 D1 保守口径把普通消息判为疑似危机——撤回命令不得受其牵连。
        processor.safety.judge = ModelLLMJudge(BrokenJudgeClient())
        processor.limiter = RateLimiter(0)  # 入站额度耗尽：普通事件必被限流

        assert await processor.handle(private_event(USER, STOP_CMD))
        assert store.consent_state(USER, now=START) == "none"
        assert messages(calls)[-1] == STOP_REPLY

        # 恢复 judge 后探限流：普通消息在限流 Stage 被静默丢弃、calls 无
        # 新增，证明限流确实生效，上面的撤回是绕过限流的前置分支。
        # （judge 故障期间普通消息会先走危机通道，无法隔离限流，故须恢复。）
        processor.safety.judge = healthy_judge
        before = len(calls)
        assert not await processor.handle(private_event(USER, "普通消息一则"))
        assert len(calls) == before

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6. 排队 → 撤回 → 触发 → 零发送（真 gate + 真 scheduler）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_enqueued_care_is_never_sent_after_stop(setup_app, tmp_path):
    app, calls, _, _ = setup_app
    state = {"now": START}

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)
        _, scheduler = build_gate_scheduler(processor, store, state, enabled=True)

        assert await processor.handle(private_event(USER, OPT_IN_CMD))
        assert store.consent_state(USER, now=START) == "active"
        assert scheduler.enqueue(USER, "p6-care-stop", START, due_at=START)
        assert scheduler.pending_count(USER) == 1

        assert await processor.handle(private_event(USER, STOP_CMD))
        assert store.consent_state(USER, now=START) == "none"
        assert scheduler.pending_count(USER) == 0

        assert await scheduler.run_due(START) == 0
        assert REMINDER not in messages(calls)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 7. enabled=false：门恒关零发送，同意保留
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_disabled_proactive_care_gate_never_sends(setup_app, tmp_path):
    app, calls, _, _ = setup_app
    state = {"now": START}

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)
        _, scheduler = build_gate_scheduler(processor, store, state, enabled=False)

        assert await processor.handle(private_event(USER, OPT_IN_CMD))
        assert scheduler.enqueue(USER, "p6-care-disabled", START, due_at=START)

        await scheduler.run_due(START)
        assert REMINDER not in messages(calls)
        # 只是不发：同意本身仍在，未被开关误伤。
        assert store.consent_state(USER, now=START) == "active"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 8. Stage 0 预筛分道断言
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [OPT_IN_CMD, PAUSE_CMD, STOP_CMD, CLOSE_CMD])
def test_care_commands_classify_as_control_lane(command):
    assert classify_lane(command, keyword_match=lambda _text: None) == "control"


def test_ordinary_text_classifies_as_ordinary_lane():
    assert classify_lane("今天天气怎么样", keyword_match=lambda _text: None) == "ordinary"


# ---------------------------------------------------------------------------
# 9. 发送中撤回不重试（降级集成版：假 gate 阻塞期间 invalidate）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_invalidate_during_inflight_send_is_not_retried():
    class BlockingGate:
        """send 阻塞在 release 上，供测试在发送途中执行撤回。"""

        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send(self, user_id, work_id, created_at):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return True

    async def scenario():
        gate = BlockingGate()
        scheduler = CareScheduler(gate, clock=lambda: START)
        assert scheduler.enqueue(USER, "p6-inflight-1", START, due_at=START)
        task = asyncio.create_task(scheduler.run_due(START))
        await asyncio.wait_for(gate.started.wait(), timeout=5)
        assert gate.calls == 1
        # 发送途中撤回：等价于 processor 侧 stop/forget 的 invalidate 半段。
        removed = scheduler.invalidate(USER)
        assert isinstance(removed, int) and removed >= 0
        gate.release.set()
        await asyncio.wait_for(task, timeout=5)
        # 已发出的这一条不回滚，但绝不重试：再次触发 send 次数仍为 1。
        await scheduler.run_due(START)
        return gate.calls

    assert asyncio.run(scenario()) == 1


# ---------------------------------------------------------------------------
# 9b. E 路修复 3：真 gate + 调度 fence（detect 窗口内的换代/撤回）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_care_fence_blocks_send_when_generation_advances_during_detect(
    setup_app, tmp_path
):
    """judge 阻塞期间新消息推进投递代次：旧关怀经 fence 拦下、零发送。"""
    from companion.safety import LLMJudge, SafetyGateway

    app, calls, _, _ = setup_app
    state = {"now": START}

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)
        store.opt_in(USER, now=START, expires=START.replace(day=31))

        judge_entered = asyncio.Event()
        judge_release = asyncio.Event()

        class BlockingJudge(LLMJudge):
            async def judge(self, text):
                judge_entered.set()
                await judge_release.wait()
                return False

        blocking_gateway = SafetyGateway([], BlockingJudge())
        gate = CareSendGate(
            store,
            processor.delivery,
            whitelist=processor.care_whitelist,
            current_safety=lambda uid: (blocking_gateway, True, 0),
            config={"proactive_care": {"enabled": True}},
            clock=lambda: state["now"],
        )
        scheduler = CareScheduler(
            gate,
            clock=lambda: state["now"],
            generation_of=processor.ledger.delivery_generation,
        )
        generation = await processor.ledger.delivery_generation(USER)
        assert scheduler.enqueue(USER, "fence-gen", START, due_at=START,
                                 generation=generation)
        task = asyncio.create_task(scheduler.run_due(START))
        await asyncio.wait_for(judge_entered.wait(), timeout=5)
        # detect 阻塞期间新消息到达：代次推进（生产口径即 dispatch 的
        # advance；此处直接推进台账代次模拟同一事实）。
        await processor.ledger.advance_delivery_generation(USER)
        judge_release.set()
        await asyncio.wait_for(task, timeout=5)
        assert REMINDER not in messages(calls)

    asyncio.run(scenario())


@pytest.mark.timeout(10)
def test_care_fence_blocks_send_when_stop_and_reoptin_during_detect(
    setup_app, tmp_path
):
    """judge 阻塞期间 stop（epoch 推进）后重新 opt-in：同意恢复 active
    也救不回旧关怀——fence 按 epoch 戳记拦下、零发送；纯 stop 的既有
    拦截（test_enqueued_care_is_never_sent_after_stop）不因此回归。
    """
    from companion.safety import LLMJudge, SafetyGateway

    app, calls, _, _ = setup_app
    state = {"now": START}

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)
        store.opt_in(USER, now=START, expires=START.replace(day=31))

        judge_entered = asyncio.Event()
        judge_release = asyncio.Event()

        class BlockingJudge(LLMJudge):
            async def judge(self, text):
                judge_entered.set()
                await judge_release.wait()
                return False

        blocking_gateway = SafetyGateway([], BlockingJudge())
        gate = CareSendGate(
            store,
            processor.delivery,
            whitelist=processor.care_whitelist,
            current_safety=lambda uid: (blocking_gateway, True, 0),
            config={"proactive_care": {"enabled": True}},
            clock=lambda: state["now"],
        )
        scheduler = CareScheduler(gate, clock=lambda: state["now"])
        processor.care_scheduler = scheduler
        generation = await processor.ledger.delivery_generation(USER)
        assert scheduler.enqueue(USER, "fence-epoch", START, due_at=START,
                                 generation=generation)
        task = asyncio.create_task(scheduler.run_due(START))
        await asyncio.wait_for(judge_entered.wait(), timeout=5)
        # detect 阻塞期间：走真实 stop 命令半段（落库删除 + invalidate
        # 推进 epoch），随后重新 opt-in 使同意回到 active。
        processor._apply_care_command(USER, STOP_CMD)
        assert store.consent_state(USER, now=START) == "none"
        store.opt_in(USER, now=START, expires=START.replace(day=31))
        assert store.consent_state(USER, now=START) == "active"
        judge_release.set()
        await asyncio.wait_for(task, timeout=5)
        assert REMINDER not in messages(calls)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 10. Review 回归 P3-2：并发「忘掉」与 opt-in 线性化（后到命令不得被吞）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_forget_and_opt_in_are_linearized(setup_app, tmp_path):
    app, calls, _, _ = setup_app
    state = {"now": START}

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)
        original_forget = processor.conversation.forget
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_forget(user_id):
            entered.set()
            await release.wait()
            return await original_forget(user_id)

        processor.conversation.forget = blocking_forget
        forget_task = asyncio.create_task(processor.handle(private_event(USER, "忘掉")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        # 忘掉已持 _state_locks 并阻塞在会话清理处：此时到达的 opt-in
        # 必须排队等待（与忘掉线性化），不得先写库再被忘掉吞掉。
        optin_task = asyncio.create_task(
            processor.handle(private_event(USER, OPT_IN_CMD))
        )
        await asyncio.sleep(0.2)
        assert not optin_task.done()
        release.set()
        await asyncio.wait_for(forget_task, timeout=5)
        await asyncio.wait_for(optin_task, timeout=5)
        processor.conversation.forget = original_forget
        # 顺序即结果：忘掉先行、opt-in 后行，最终态为已开启。
        assert store.consent_state(USER, now=START) == "active"
        assert messages(calls) == [FORGET_REPLY, OPT_IN_REPLY]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 11. Review 回归 P2-2：危机处置进行中（告警未回）关怀零发送
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_care_not_sent_while_crisis_response_pending(setup_app, tmp_path):
    app, calls, _, _ = setup_app
    state = {"now": START}

    class BlockingAlerter:
        """alert 阻塞即「危机处置进行中」窗口（告警未确认）。"""

        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.items = []

        async def alert(self, user_id, reason, summary="sensitive text omitted"):
            self.items.append((user_id, reason))
            self.started.set()
            await self.release.wait()

    async def scenario():
        processor, store = wire_care(app, tmp_path, state)
        await processor.conversation.mark_onboarded(USER)
        _, scheduler = build_gate_scheduler(processor, store, state, enabled=True)
        assert await processor.handle(private_event(USER, OPT_IN_CMD))
        assert scheduler.enqueue(USER, "p6-crisis-1", START, due_at=START)

        blocking = BlockingAlerter()
        processor.alerter = blocking
        crisis_task = asyncio.create_task(
            processor.handle(private_event(USER, "活着没意思"))
        )
        await asyncio.wait_for(blocking.started.wait(), timeout=5)
        # 危机态在任何 await 之前已同步落账：实时重查必须判不允许。
        _, allowed, _ = processor.current_care_safety(USER)
        assert allowed is False
        # 关怀触发被门挡下：零 REMINDER 发出。
        await scheduler.run_due(START)
        assert REMINDER not in messages(calls)
        blocking.release.set()
        await asyncio.wait_for(crisis_task, timeout=5)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 12. Review 回归 P3-3/P3-4：create_app 组合根接线 + lifespan 可重复进入
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_create_app_wires_shared_delivery_and_scheduler(setup_app, tmp_path):
    from dataclasses import replace

    from companion.brain import CompanionBrain
    from companion.ingress import create_app

    _, _, _, settings = setup_app
    configured = replace(
        settings,
        app={
            **settings.app,
            "proactive_care": {
                "enabled": True,
                "store_path": str(tmp_path / "wired-care.db"),
                "check_interval_seconds": 900,
            },
        },
    )

    class FakeSender:
        async def send_private_msg(self, user_id, message, *, priority="normal"):
            return {"status": "ok"}

        async def send_alert_private_msg(self, user_id, message):
            return {"status": "ok"}

    class FakeLLM:
        async def generate(self, _prompt):
            return "测试回复"

        async def generate_json(self, _prompt):
            return '{"bubbles": ["测试回复"]}'

    class FakeAlerter:
        async def alert(self, user_id, reason, summary="sensitive text omitted"):
            return None

    async def no_sleep(_delay):
        return None

    app2 = create_app(
        configured,
        sender=FakeSender(),
        alerter=FakeAlerter(),
        sleep=no_sleep,
        random_uniform=lambda _a, _b: 0,
        brain=CompanionBrain(configured.persona, FakeLLM()),
    )
    processor2 = app2.state.processor
    # 组合根装配：存储按 store_path 落盘装配、门与调度器挂到 processor。
    assert processor2.care_preferences is not None
    assert processor2.care_preferences.path == (tmp_path / "wired-care.db").resolve()
    assert app2.state.care_gate is processor2.care_gate
    # 契约 3：门与回复路径共享同一 DeliveryService（同一额度账本）。
    assert app2.state.care_gate.delivery is processor2.delivery
    assert app2.state.care_scheduler is processor2.care_scheduler
    # 契约 4：代次回调确实指向 ledger（新消息推进代次即失效未发关怀）。
    assert (
        app2.state.care_scheduler._generation_of
        == processor2.ledger.delivery_generation
    )

    async def scenario():
        async with app2.router.lifespan_context(app2):
            status = app2.state.scheduler.status()
            assert status["care"]["running"] is True
            assert "connection_health" not in status
        # P3-4：lifespan 第二次进入不得因重名注册抛 ValueError。
        async with app2.router.lifespan_context(app2):
            assert app2.state.scheduler.status()["care"]["running"] is True

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 13. Review 第 2 轮 P2 回归：告警节奏锚点不得被危机消息刷新
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_crisis_alert_cadence_uses_first_alert_anchor(setup_app, monkeypatch):
    """crisis_history.time 是告警去重锚点：只在首次/升级/窗口过期时重置。

    关怀阻塞时钟（_last_crisis_at）与它分开记账；若把占位记录写进
    crisis_history，每条危机消息都会刷新 time，持续同级危机在窗口
    后永远不再告警（第 2 轮 Review 动态复现的漂移）。本用例以假
    time.time 驱动：t 告警、t+1800 不告警、t+5000（距锚点 ≥3600）
    必须再次告警。
    """
    app, calls, alerter, _ = setup_app
    state = {"now": 1_000_000.0}
    monkeypatch.setattr("time.time", lambda: state["now"])

    async def scenario():
        processor = app.state.processor
        await processor.conversation.mark_onboarded(USER)
        await processor.handle(private_event(USER, "想自杀"))
        assert len(alerter.items) == 1
        state["now"] += 1800  # 窗口内同级重复：不重复告警
        await processor.handle(private_event(USER, "想自杀"))
        assert len(alerter.items) == 1
        state["now"] += 3200  # 距首次告警共 5000s ≥ 3600：窗口过期，再告警
        await processor.handle(private_event(USER, "想自杀"))
        assert len(alerter.items) == 2

    asyncio.run(scenario())
