"""P6 调度包测试：进程内调度器 + 关怀待发队列（假 gate / 假时钟）。"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from companion.scheduler import CareScheduler, InProcessScheduler

USER = "10101"
OTHER = "20202"
T0 = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)


def make_fake_sleep(record):
    """假 sleep：记录延迟后只让出一次事件循环，不真实等待。"""

    async def fake_sleep(delay):
        record.append(delay)
        await asyncio.sleep(0)

    return fake_sleep


class FakeGate:
    """记录 send 调用的假关怀发送门，可配置返回值/异常/阻塞。"""

    def __init__(self, *, result=True, exc=None, blocker=None):
        self.calls = []
        self.result = result
        self.exc = exc
        self.blocker = blocker
        self.entered = None  # 需要时由测试在事件循环内赋 asyncio.Event()

    async def send(self, user_id, work_id, created_at):
        self.calls.append((user_id, work_id, created_at))
        if self.entered is not None:
            self.entered.set()
        if self.blocker is not None:
            await self.blocker.wait()
        if self.exc is not None:
            raise self.exc
        return self.result


# ---------------------------------------------------------------------------
# InProcessScheduler
# ---------------------------------------------------------------------------


def test_scheduler_runs_runner_periodically_with_returned_delay():
    async def scenario():
        delays = []
        sched = InProcessScheduler(sleep=make_fake_sleep(delays))
        calls = 0
        reached = asyncio.Event()

        async def runner():
            nonlocal calls
            calls += 1
            if calls >= 5:
                reached.set()
            return 0.25

        sched.register("job", runner)
        await sched.start()
        await asyncio.wait_for(reached.wait(), timeout=1)
        await sched.stop()

        assert calls >= 5
        assert delays and all(delay == 0.25 for delay in delays)
        status = sched.status()["job"]
        assert status["running"] is False
        assert status["runs"] >= 5
        assert status["last_error"] is None

    asyncio.run(scenario())


def test_scheduler_runner_exception_does_not_kill_loop():
    async def scenario():
        delays = []
        sched = InProcessScheduler(sleep=make_fake_sleep(delays))
        calls = 0
        reached = asyncio.Event()

        async def runner():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise RuntimeError("sensitive body text must not be recorded")
            if calls >= 5:
                reached.set()
            return 0.1

        sched.register("flaky", runner, interval=0.5)
        await sched.start()
        await asyncio.wait_for(reached.wait(), timeout=1)
        await sched.stop()

        status = sched.status()["flaky"]
        # 只记异常类型名，不记 str(exc)。
        assert status["last_error"] == "RuntimeError"
        assert status["runs"] >= 5
        # 异常后按登记 interval 继续等待，而不是死循环或退出。
        assert delays.count(0.5) == 3

    asyncio.run(scenario())


def test_scheduler_single_run_when_delay_and_interval_are_none():
    async def scenario():
        sched = InProcessScheduler(sleep=make_fake_sleep([]))
        calls = 0
        ran = asyncio.Event()

        async def runner():
            nonlocal calls
            calls += 1
            ran.set()
            return None

        sched.register("once", runner)
        await sched.start()
        await asyncio.wait_for(ran.wait(), timeout=1)
        # 让单次任务的循环有机会自行收尾。
        for _ in range(5):
            await asyncio.sleep(0)

        assert calls == 1
        status = sched.status()["once"]
        assert status == {"running": False, "runs": 1, "last_error": None}
        await sched.stop()

    asyncio.run(scenario())


def test_scheduler_stop_cancels_blocked_runner_and_runs_finally():
    async def scenario():
        sched = InProcessScheduler()
        entered = asyncio.Event()
        cleaned = []

        async def runner():
            entered.set()
            try:
                await asyncio.Event().wait()  # 永不返回
            finally:
                cleaned.append(True)

        sched.register("blocked", runner)
        await sched.start()
        await asyncio.wait_for(entered.wait(), timeout=1)

        started_at = time.monotonic()
        await sched.stop(timeout=10.0)
        elapsed = time.monotonic() - started_at

        # 先 cancel 再收拢：远在 timeout 之前返回，finally 立即执行。
        assert elapsed < 2.0
        assert cleaned == [True]
        assert sched.status()["blocked"]["running"] is False

    asyncio.run(scenario())


def test_scheduler_stop_timeout_does_not_raise_when_runner_swallows_cancel():
    async def scenario():
        sched = InProcessScheduler()
        entered = asyncio.Event()
        cleaned = []
        swallowed = False

        async def runner():
            nonlocal swallowed
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallowed:
                    swallowed = True
                    await asyncio.Event().wait()
                raise
            finally:
                cleaned.append(True)

        sched.register("stubborn", runner)
        await sched.start()
        await asyncio.wait_for(entered.wait(), timeout=1)

        started_at = time.monotonic()
        await sched.stop(timeout=0.05)  # 超时不抛、直接返回
        assert time.monotonic() - started_at < 2.0

        await sched.stop(timeout=1.0)
        assert cleaned == [True]

    asyncio.run(scenario())


def test_scheduler_start_is_idempotent():
    async def scenario():
        sched = InProcessScheduler()
        entries = 0
        entered = asyncio.Event()

        async def runner():
            nonlocal entries
            entries += 1
            entered.set()
            await asyncio.Event().wait()

        sched.register("job", runner)
        await sched.start()
        await sched.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert entries == 1
        await sched.stop()

    asyncio.run(scenario())


def test_scheduler_duplicate_register_and_stop_before_start():
    sched = InProcessScheduler()

    async def runner():
        return None

    sched.register("job", runner)
    with pytest.raises(ValueError):
        sched.register("job", runner)

    # 未 start 时 stop 安全返回，可重复调用。
    asyncio.run(sched.stop())
    asyncio.run(sched.stop())
    assert sched.status()["job"] == {
        "running": False,
        "runs": 0,
        "last_error": None,
    }


# ---------------------------------------------------------------------------
# E 路修复 5：InProcessScheduler 运行代次
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_scheduler_stop_wins_over_runner_that_swallows_cancel_and_returns():
    """runner 吞掉 CancelledError 并正常返回：stop 后不得被再次调用。

    旧实现 _loop 不查停止状态，runner 一返回就按延迟再次调用它，
    stop() 返回后调用次数仍增长。运行代次机制下，stop 先推进代次，
    吞取消返回的 runner 在再次调用前被代次校验拦下退出。
    """

    async def scenario():
        sched = InProcessScheduler()
        calls = 0
        entered = asyncio.Event()

        async def runner():
            nonlocal calls
            calls += 1
            entered.set()
            try:
                await asyncio.Event().wait()  # 等取消
            except asyncio.CancelledError:
                return 0.01  # 吞掉取消并正常返回（带延迟）
            return 0.01

        sched.register("swallower", runner)
        await sched.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        await sched.stop(timeout=2.0)
        assert calls == 1
        # stop 返回后再观察一段时间：旧循环不得复活、不得再调 runner。
        await asyncio.sleep(0.1)
        assert calls == 1
        assert sched.status()["swallower"]["running"] is False

        # stop → start：已结束的 entry 任务须被重新拉起、恢复运行。
        entered.clear()
        await sched.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert calls == 2
        await sched.stop(timeout=2.0)
        assert calls == 2

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# CareScheduler
# ---------------------------------------------------------------------------


def test_care_enqueue_dedup_and_pending_count():
    gate = FakeGate()
    sched = CareScheduler(gate, clock=lambda: T0)

    assert sched.enqueue(USER, "work-1", T0) is True
    assert sched.enqueue(USER, "work-1", T0) is False
    assert sched.enqueue(USER, "work-2", T0) is True
    assert sched.enqueue(OTHER, "work-1", T0) is True

    assert sched.pending_count() == 3
    assert sched.pending_count(USER) == 2
    assert sched.pending_count(OTHER) == 1
    assert gate.calls == []


def test_care_run_due_not_due_then_due_sends_in_due_order_and_consumes():
    gate = FakeGate()
    sched = CareScheduler(gate, clock=lambda: T0)
    sched.enqueue(USER, "late", T0, due_at=T0 + timedelta(seconds=10))
    sched.enqueue(USER, "early", T0, due_at=T0)

    # 未到期：零发送、项保留。
    assert asyncio.run(sched.run_due(T0 - timedelta(seconds=1))) == 0
    assert gate.calls == []
    assert sched.pending_count() == 2

    # 到期：按 due_at 升序发送，发送后消费。
    assert asyncio.run(sched.run_due(T0 + timedelta(seconds=10))) == 2
    assert [call[1] for call in gate.calls] == ["early", "late"]
    assert gate.calls[0] == (USER, "early", T0)
    assert sched.pending_count() == 0
    assert asyncio.run(sched.run_due(T0 + timedelta(seconds=10))) == 0


def test_care_invalidate_before_run_due_sends_nothing():
    gate = FakeGate()
    sched = CareScheduler(gate, clock=lambda: T0)
    sched.enqueue(USER, "work-1", T0)
    sched.enqueue(USER, "work-2", T0)
    sched.enqueue(OTHER, "work-1", T0)

    # 排队 → 撤回 → 触发 → 零发送（核心用例）。
    assert sched.invalidate(USER) == 2
    assert sched.pending_count(USER) == 0

    assert asyncio.run(sched.run_due(T0)) == 1  # 只剩 OTHER 的一项
    assert [call[0] for call in gate.calls] == [OTHER]


def test_care_invalidate_then_reenqueue_old_dropped_new_sent():
    gate = FakeGate()
    sched = CareScheduler(gate, clock=lambda: T0)
    sched.enqueue(USER, "old", T0)
    sched.invalidate(USER)

    # 撤回后重新入队：戳记新 epoch；旧项已丢弃、新项可发。
    assert sched.enqueue(USER, "old", T0) is True
    assert sched.enqueue(USER, "new", T0) is True
    assert asyncio.run(sched.run_due(T0)) == 2
    assert [call[1] for call in gate.calls] == ["old", "new"]


def test_care_generation_stamp_mismatch_drops_and_match_sends():
    gate = FakeGate()
    current = {"generation": 2}

    async def generation_of(user_id):
        return current["generation"]

    sched = CareScheduler(gate, clock=lambda: T0, generation_of=generation_of)
    sched.enqueue(USER, "stale", T0, generation=1)
    sched.enqueue(USER, "fresh", T0, generation=2)

    assert asyncio.run(sched.run_due(T0)) == 1
    assert [call[1] for call in gate.calls] == ["fresh"]
    assert sched.pending_count() == 0


def test_care_gate_false_consumes_and_never_retries():
    gate = FakeGate(result=False)
    sched = CareScheduler(gate, clock=lambda: T0)
    sched.enqueue(USER, "work-1", T0)

    assert asyncio.run(sched.run_due(T0)) == 1
    assert sched.pending_count() == 0
    # 第二次触发不重试：send 调用次数不增。
    assert asyncio.run(sched.run_due(T0)) == 0
    assert len(gate.calls) == 1


def test_care_gate_exception_consumes_and_never_retries():
    gate = FakeGate(exc=RuntimeError("sensitive send detail"))
    sched = CareScheduler(gate, clock=lambda: T0)
    sched.enqueue(USER, "work-1", T0)

    assert asyncio.run(sched.run_due(T0)) == 1
    assert sched.last_error == "RuntimeError"
    assert sched.pending_count() == 0
    assert asyncio.run(sched.run_due(T0)) == 0
    assert len(gate.calls) == 1


def test_care_invalidate_while_send_blocked():
    async def scenario():
        gate = FakeGate()
        gate.entered = asyncio.Event()
        gate.blocker = asyncio.Event()
        sched = CareScheduler(gate, clock=lambda: T0)
        sched.enqueue(USER, "work-1", T0)

        task = asyncio.create_task(sched.run_due(T0))
        await asyncio.wait_for(gate.entered.wait(), timeout=1)

        # 发送中撤回：该项已在发送前消费，队列为空；释放后发送完成，
        # 后续触发零发送、绝不重发。
        sched.invalidate(USER)
        assert sched.pending_count() == 0
        gate.blocker.set()

        assert await asyncio.wait_for(task, timeout=1) == 1
        assert len(gate.calls) == 1
        assert sched.pending_count() == 0
        assert await sched.run_due(T0) == 0
        assert len(gate.calls) == 1

    asyncio.run(scenario())


def test_care_tick_runs_due_and_returns_float_interval():
    gate = FakeGate()
    sched = CareScheduler(gate, clock=lambda: T0)
    sched.enqueue(USER, "work-1", T0)

    result = asyncio.run(sched.tick(30))
    assert result == 30.0
    assert isinstance(result, float)
    assert len(gate.calls) == 1


# ---------------------------------------------------------------------------
# Review 回归（P6 降级 Review 第 1 轮）
# ---------------------------------------------------------------------------


@pytest.mark.timeout(10)
def test_stop_hard_timeout_even_if_runner_swallows_cancellation():
    """P2-1：runner 持续吞掉 CancelledError 时 stop 仍须在 timeout 返回。

    旧实现用 wait_for(gather(...))：gather 必须等全部子任务真正结束，
    吞取消的任务永不结束 → stop 突破上限（Review 实测复现）。现实现以
    asyncio.wait 收拢，到点即返回。
    """

    async def scenario():
        sched = InProcessScheduler()
        started = asyncio.Event()
        swallow = {"on": True}

        async def stubborn():
            started.set()
            while True:
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    if not swallow["on"]:
                        raise
                    continue  # 持续吞取消：任务自身永不结束

        sched.register("stubborn", stubborn)
        before = asyncio.all_tasks()
        await sched.start()
        (task,) = asyncio.all_tasks() - before
        await asyncio.wait_for(started.wait(), timeout=1)
        begin = time.monotonic()
        await sched.stop(timeout=0.2)
        elapsed = time.monotonic() - begin
        # 收尾：关掉吞取消开关并再取消一次，让任务真正结束，避免它
        # 在 asyncio.run 关闭事件循环时同样拒绝收拢而挂死整个用例。
        swallow["on"] = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return elapsed

    assert asyncio.run(scenario()) < 2.0


@pytest.mark.timeout(10)
def test_run_due_drops_unstamped_item_when_generation_wired():
    """P3-1：接线 generation_of 后，缺 generation 戳记的项 fail closed。

    入队方漏传 generation 不得静默退化为「新消息不失效关怀」：触发时
    直接丢弃、零发送；带正确戳记的项照常发送。
    """

    async def scenario():
        gate = FakeGate()
        current = {"generation": 7}

        async def generation_of(_user_id):
            return current["generation"]

        sched = CareScheduler(gate, clock=lambda: T0, generation_of=generation_of)
        assert sched.enqueue(USER, "unstamped", T0)
        assert sched.enqueue(USER, "stamped", T0, generation=7)
        sent = await sched.run_due(T0)
        assert sent == 1
        assert [call[1] for call in gate.calls] == ["stamped"]
        assert sched.pending_count() == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# E 路修复 3：CareScheduler fence 闭包（发送中复核 epoch/代次）
# ---------------------------------------------------------------------------


class FenceAwareGate:
    """接受 fence 的假 gate：阻塞模拟 detect 窗口，释放后先过 fence 才记发送。"""

    def __init__(self):
        self.calls = []
        self.sent = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, user_id, work_id, created_at, *, fence=None):
        self.calls.append((user_id, work_id))
        self.entered.set()
        await self.release.wait()
        if fence is not None and not await fence():
            return False
        self.sent.append((user_id, work_id))
        return True


@pytest.mark.timeout(10)
def test_care_fence_blocks_when_generation_advances_during_send():
    """gate 阻塞（detect 窗口）期间投递代次推进：fence 拦下、零发送。"""

    async def scenario():
        gate = FenceAwareGate()
        current = {"generation": 3}

        async def generation_of(_user_id):
            return current["generation"]

        sched = CareScheduler(gate, clock=lambda: T0, generation_of=generation_of)
        assert sched.enqueue(USER, "work-gen", T0, generation=3)
        task = asyncio.create_task(sched.run_due(T0))
        await asyncio.wait_for(gate.entered.wait(), timeout=1)
        current["generation"] = 4  # 新消息推进代次
        gate.release.set()
        assert await asyncio.wait_for(task, timeout=1) == 1  # gate 被调用过
        assert gate.sent == []  # 但 fence 拦下，实际零发送

    asyncio.run(scenario())


@pytest.mark.timeout(10)
def test_care_fence_blocks_when_epoch_advances_during_send():
    """gate 阻塞期间 stop→重新入队（epoch 推进）：旧项 fence 不过、零发送。"""

    async def scenario():
        gate = FenceAwareGate()
        sched = CareScheduler(gate, clock=lambda: T0)
        assert sched.enqueue(USER, "work-epoch", T0)
        task = asyncio.create_task(sched.run_due(T0))
        await asyncio.wait_for(gate.entered.wait(), timeout=1)
        # stop 的调度半段（invalidate 推进 epoch）+ 重新 opt-in 后的新项。
        sched.invalidate(USER)
        assert sched.enqueue(USER, "work-epoch-new", T0)
        gate.release.set()
        assert await asyncio.wait_for(task, timeout=1) == 1
        assert gate.sent == []
        # 新项（新 epoch 戳记）不受旧撤回影响，正常发送。
        gate.entered.clear()
        gate.release.clear()
        task2 = asyncio.create_task(sched.run_due(T0))
        await asyncio.wait_for(gate.entered.wait(), timeout=1)
        gate.release.set()
        assert await asyncio.wait_for(task2, timeout=1) == 1
        assert gate.sent == [(USER, "work-epoch-new")]

    asyncio.run(scenario())


@pytest.mark.timeout(10)
def test_care_fence_passes_when_nothing_changes_during_send():
    """对照组：窗口内无撤回/换代时 fence 放行、正常发送（不误杀）。"""

    async def scenario():
        gate = FenceAwareGate()

        async def generation_of(_user_id):
            return 5

        sched = CareScheduler(gate, clock=lambda: T0, generation_of=generation_of)
        assert sched.enqueue(USER, "work-ok", T0, generation=5)
        task = asyncio.create_task(sched.run_due(T0))
        await asyncio.wait_for(gate.entered.wait(), timeout=1)
        gate.release.set()
        assert await asyncio.wait_for(task, timeout=1) == 1
        assert gate.sent == [(USER, "work-ok")]

    asyncio.run(scenario())
