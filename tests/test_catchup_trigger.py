"""RecoveryWatcher 触发状态机测试（设计 §9 / §4.1–§4.3，覆盖 AC1–AC5 触发侧）。

手法钉死：假 health 对象（snapshot / stable_since / clock 可控）+ 假
wall_clock + 计数型 stub service（带 stub scan_store）驱动 ``poll_once()``，
断言触发计数与窗口值。扫描行为侧 AC 由 tests/test_catchup_scan.py 覆盖。
"""

import asyncio

from companion.ingress.catchup import (
    MAX_WINDOW_SECONDS,
    RecoveryWatcher,
)
from companion.ops.connection_health import STABLE_SECONDS, Snapshot


def _snapshot(*, qq="reported_online", ingress="ready", companion="active", napcat="active"):
    return Snapshot(
        companion_process=companion,
        napcat_process=napcat,
        companion_result="success",
        napcat_result="success",
        ingress=ingress,
        onebot="reachable",
        qq=qq,
        action="none",
    )


HEALTHY = _snapshot()
UNHEALTHY = _snapshot(qq="offline")


class FakeHealth:
    """鸭子类型健康对象：snapshot / stable_since / clock 全可控。"""

    def __init__(self):
        self.snapshot = None
        self.stable_since = None
        self.mono = 0.0

    def clock(self):
        return self.mono

    def go_healthy(self):
        self.snapshot = HEALTHY
        if self.stable_since is None:
            self.stable_since = self.mono

    def go_unhealthy(self):
        self.snapshot = UNHEALTHY
        self.stable_since = None

    def stabilize(self):
        """推进单调钟到刚好稳定满 STABLE_SECONDS。"""
        assert self.stable_since is not None
        self.mono = self.stable_since + STABLE_SECONDS


class StubScanStore:
    def __init__(self, latest=0.0):
        self.latest = latest
        self.begun = []

    def latest_window_end(self):
        return self.latest

    def try_begin(self, window_start, window_end):
        self.begun.append((window_start, window_end))
        self.latest = max(self.latest, window_end)
        return True

    def complete(self, window_start, window_end, outcome):
        return True


class CountingService:
    """计数型 stub service：记录每次 run_scan 的窗口，可阻塞模拟在途扫描。"""

    def __init__(self, latest=0.0):
        self.scan_store = StubScanStore(latest)
        self.calls = []
        self.release = None  # asyncio.Event：设置后 run_scan 阻塞至其 set

    async def run_scan(self, window_start, window_end):
        self.calls.append((window_start, window_end))
        self.scan_store.latest = max(self.scan_store.latest, window_end)
        if self.release is not None:
            await self.release.wait()
        return None


class Rig:
    def __init__(self, latest=0.0):
        self.health = FakeHealth()
        self.service = CountingService(latest)
        self.wall = [0.0]
        self.watcher = RecoveryWatcher(
            lambda: self.health,
            self.service,
            wall_clock=lambda: self.wall[0],
        )

    async def poll(self):
        result = await self.watcher.poll_once()
        await self.watcher.wait_idle()
        return result

    async def recover(self, down_at, recovered_at):
        """走完一次「断线 → 恢复 → 稳定」转移并轮询一轮。"""
        self.wall[0] = down_at
        self.health.go_unhealthy()
        await self.watcher.poll_once()
        self.wall[0] = recovered_at
        self.health.go_healthy()
        self.health.stabilize()
        return await self.poll()


# ---------------------------------------------------------------------------
# AC1：稳定 <120s 零触发；≥120s 恰好触发一次
# ---------------------------------------------------------------------------


def test_ac1_no_trigger_before_stable_then_exactly_one():
    async def scenario():
        rig = Rig()
        rig.wall[0] = 10_000.0
        rig.health.go_unhealthy()
        assert await rig.watcher.poll_once() is False
        rig.wall[0] = 10_300.0
        rig.health.go_healthy()
        # 稳定 119s：不触发
        rig.health.mono = rig.health.stable_since + STABLE_SECONDS - 1.0
        assert await rig.poll() is False
        assert rig.service.calls == []
        # 稳定满 120s：恰好触发一次，窗口 = [down_since, recovered_at]
        rig.health.stabilize()
        assert await rig.poll() is True
        assert rig.service.calls == [(10_000.0, 10_300.0)]
        # 同一转移重复采样不再触发
        assert await rig.poll() is False
        assert len(rig.service.calls) == 1

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# AC2：同一转移只扫一轮；再次断线恢复再触发一轮
# ---------------------------------------------------------------------------


def test_ac2_second_transition_triggers_again():
    async def scenario():
        rig = Rig()
        assert await rig.recover(1_000.0, 1_300.0) is True
        assert await rig.recover(5_000.0, 5_300.0) is True
        assert rig.service.calls == [(1_000.0, 1_300.0), (5_000.0, 5_300.0)]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# AC3：始终非 healthy / QQ 非 reported_online / 健康未装配 → 零触发
# ---------------------------------------------------------------------------


def test_ac3_never_healthy_zero_triggers():
    async def scenario():
        rig = Rig()
        rig.health.snapshot = None  # 尚未首测
        assert await rig.poll() is False
        rig.health.go_unhealthy()
        for wall in (100.0, 200.0, 300.0):
            rig.wall[0] = wall
            assert await rig.poll() is False
        assert rig.service.calls == []

    asyncio.run(scenario())


def test_ac3_qq_not_reported_online_zero_triggers():
    async def scenario():
        rig = Rig()
        # ingress ready、双进程 active，但 QQ 未 reported_online：不健康
        rig.health.snapshot = _snapshot(qq="unconfirmed")
        rig.health.stable_since = 0.0
        rig.health.mono = STABLE_SECONDS + 10.0
        rig.wall[0] = 500.0
        assert await rig.poll() is False
        assert rig.service.calls == []

    asyncio.run(scenario())


def test_ac3_health_provider_none_zero_triggers():
    async def scenario():
        service = CountingService()
        watcher = RecoveryWatcher(lambda: None, service, wall_clock=lambda: 1_000.0)
        assert await watcher.poll_once() is False
        assert service.calls == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# AC4：窗口钳制——宽度 ≤24h 且下限不早于 latest_window_end
# ---------------------------------------------------------------------------


def test_ac4_window_clamped_to_24h():
    async def scenario():
        rig = Rig()
        down_at = 1_000.0
        recovered_at = down_at + MAX_WINDOW_SECONDS + 3_600.0  # 断线超过 24h
        assert await rig.recover(down_at, recovered_at) is True
        (start, end), = rig.service.calls
        assert end == recovered_at
        assert start == recovered_at - MAX_WINDOW_SECONDS
        assert end - start <= MAX_WINDOW_SECONDS

    asyncio.run(scenario())


def test_ac4_window_start_not_before_latest_window_end():
    async def scenario():
        rig = Rig(latest=5_000.0)
        # down_since(4_000) 早于上次已扫终点(5_000)：下限钳到 5_000
        assert await rig.recover(4_000.0, 6_000.0) is True
        assert rig.service.calls == [(5_000.0, 6_000.0)]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# AC5（watcher/store 联测）：已扫窗口不重触发
# ---------------------------------------------------------------------------


def test_ac5_scanned_window_not_retriggered():
    async def scenario():
        rig = Rig()
        assert await rig.recover(1_000.0, 1_300.0) is True
        assert len(rig.service.calls) == 1
        # 进程内同转移重复采样：不重触发（转移已消费）
        assert await rig.poll() is False
        # 模拟进程重启：新 Watcher + store 已有 latest=1_300；启动恢复被
        # latest 钳制后窗口 <1s，不重扫同一窗口
        health2 = FakeHealth()
        health2.go_healthy()
        health2.stabilize()
        watcher2 = RecoveryWatcher(
            lambda: health2, rig.service, wall_clock=lambda: 1_300.0
        )
        assert await watcher2.poll_once() is False
        await watcher2.wait_idle()
        assert len(rig.service.calls) == 1

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# §4.3 启动恢复：从未观测到断线、store 无记录 → 对最近 24h 触发一次
# ---------------------------------------------------------------------------


def test_startup_recovery_triggers_once_with_24h_window():
    async def scenario():
        rig = Rig()
        recovered_at = 200_000.0
        rig.wall[0] = recovered_at
        rig.health.go_healthy()
        rig.health.stabilize()
        assert await rig.poll() is True
        assert rig.service.calls == [
            (recovered_at - MAX_WINDOW_SECONDS, recovered_at)
        ]
        # 启动待触发已消费：重复采样不再触发
        assert await rig.poll() is False
        assert len(rig.service.calls) == 1

    asyncio.run(scenario())


def test_startup_recovery_superseded_by_observed_outage():
    async def scenario():
        rig = Rig()
        # 先观测到断线：启动恢复让位，窗口以下线时刻为下限而非 24h 前
        rig.wall[0] = 50_000.0
        rig.health.go_unhealthy()
        assert await rig.watcher.poll_once() is False
        rig.wall[0] = 51_000.0
        rig.health.go_healthy()
        rig.health.stabilize()
        assert await rig.poll() is True
        assert rig.service.calls == [(50_000.0, 51_000.0)]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 在途扫描：新转移只保留最新一个 pending 窗口，在途结束后发出
# ---------------------------------------------------------------------------


def test_pending_window_launched_after_inflight_scan():
    async def scenario():
        rig = Rig()
        rig.service.release = asyncio.Event()
        # 第一轮：触发后扫描在途（阻塞在 release 上）
        rig.wall[0] = 1_000.0
        rig.health.go_unhealthy()
        await rig.watcher.poll_once()
        rig.wall[0] = 1_300.0
        rig.health.go_healthy()
        rig.health.stabilize()
        assert await rig.watcher.poll_once() is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(rig.service.calls) == 1
        # 在途期间再次断线恢复：受理为 pending（只留最新一个），不并发
        rig.wall[0] = 2_000.0
        rig.health.go_unhealthy()
        await rig.watcher.poll_once()
        rig.wall[0] = 2_300.0
        rig.health.go_healthy()
        rig.health.stable_since = rig.health.mono
        rig.health.stabilize()
        assert await rig.watcher.poll_once() is True
        assert len(rig.service.calls) == 1
        # 放行第一轮后，等待落定：pending 窗口随即发出
        rig.service.release.set()
        await rig.watcher.wait_idle()
        assert rig.service.calls == [(1_000.0, 1_300.0), (2_000.0, 2_300.0)]

    asyncio.run(scenario())
