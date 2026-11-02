"""调度端口与进程内实现、主动关怀待发队列（方案 §5.8）。

本包是 import 边界登记的叶子包：只用标准库，不 import 任何
``companion`` 模块。关怀发送所需的门（gate）、当前投递代次等能力
全部由构造方以鸭子类型注入，本包不反向依赖 safety / memory 等层。
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

__all__ = ["CareScheduler", "InProcessScheduler", "Scheduler"]

# runner：无参 async callable，返回距下次运行的延迟秒数或 None。
Runner = Callable[[], Awaitable[float | None]]
# sleep：与 asyncio.sleep 同签名的可注入等待函数，便于假时钟测试。
SleepFn = Callable[[float], Awaitable[None]]


@runtime_checkable
class Scheduler(Protocol):
    """调度端口：注册周期 runner 并统一启停（方案 §5.8）。"""

    def register(
        self, name: str, runner: Runner, *, interval: float | None = None
    ) -> None:
        """登记一个命名任务；同名重复登记应抛 ValueError。

        runner 每次返回的延迟优先于 interval；两者皆为 None 时
        该任务只跑一次便结束。
        """
        ...

    async def start(self) -> None:
        """启动全部已登记任务；重复调用应幂等无副作用。"""
        ...

    async def stop(self, timeout: float = 10.0) -> None:
        """停止调度：先取消任务循环，再在 timeout 上限内等待收拢。"""
        ...

    def status(self) -> dict:
        """返回各任务的运行状态、已完成调用次数与最近错误类型名。"""
        ...


@dataclass
class _Entry:
    """InProcessScheduler 内部登记项：runner、周期与运行计数。"""

    name: str
    runner: Runner
    interval: float | None = None
    runs: int = 0
    last_error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)


class InProcessScheduler:
    """基于 asyncio 的进程内 Scheduler 实现。

    每个任务一个 asyncio.Task 循环：await runner() 后按其返回延迟
    （缺省时用登记 interval）sleep 再跑。runner 抛异常不会杀死循环：
    只记异常类型名到 last_error（不记 str(exc)，防正文与敏感信息
    入状态/日志），并按 interval（或默认 1.0 秒）继续循环。
    """

    def __init__(self, *, sleep: SleepFn = asyncio.sleep) -> None:
        """创建调度器；sleep 可注入以便假时钟测试，默认 asyncio.sleep。"""
        self._sleep = sleep
        self._entries: dict[str, _Entry] = {}
        self._started = False
        # 运行代次（E 路修复 5）：每次有效的 start/stop 都递增；每个
        # _loop 捕获自身代次，runner 返回/异常后代次已变即退出——吞掉
        # CancelledError 又正常返回的 runner 不得被旧循环再次调用。
        self._run_generation = 0

    def register(
        self, name: str, runner: Runner, *, interval: float | None = None
    ) -> None:
        """登记命名任务；同名重复登记抛 ValueError。

        已启动状态下登记的新任务会立即随调度器运行。
        """
        if name in self._entries:
            raise ValueError(f"duplicate scheduler task: {name}")
        entry = _Entry(name=name, runner=runner, interval=interval)
        self._entries[name] = entry
        if self._started:
            self._spawn(entry)

    async def start(self) -> None:
        """启动全部任务循环；已启动时再次调用无副作用（幂等）。"""
        if self._started:
            return
        self._run_generation += 1
        self._started = True
        for entry in self._entries.values():
            self._spawn(entry)

    async def stop(self, timeout: float = 10.0) -> None:
        """停止调度：先 cancel 全部任务循环，再限时等待收拢。

        在途 runner 调用随任务取消而中止，其 finally/cleanup 立即
        执行；发送类在途工作按 ambiguous 口径自行记账（方案 §5.3
        关停口径）。等待以 timeout 为上限，超时不抛、直接返回，
        免得永不返回的 runner 把关停拖满。未启动时调用安全返回。
        """
        # 先推进代次再取消：在途 runner 即使吞掉取消并正常返回，其
        # _loop 在再次调用前也会因代次不符退出（E 路修复 5）。
        self._run_generation += 1
        tasks = [
            entry.task
            for entry in self._entries.values()
            if entry.task is not None and not entry.task.done()
        ]
        self._started = False
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        # asyncio.wait 到点即返回，是硬上限：不像 wait_for(gather)
        # 必须等全部子任务真正结束——持续吞掉取消信号的 runner 也
        # 挡不住关停（Review P2-1 实测复现后改此实现）。未收拢的
        # 任务保持 pending，由事件循环关闭时兜底。
        await asyncio.wait(tasks, timeout=timeout)

    def status(self) -> dict:
        """返回 {name: {"running": bool, "runs": int, "last_error": str | None}}。

        runs 为已完成的 runner 调用次数（抛异常的调用也算完成一次）。
        """
        return {
            name: {
                "running": entry.task is not None and not entry.task.done(),
                "runs": entry.runs,
                "last_error": entry.last_error,
            }
            for name, entry in self._entries.items()
        }

    def _spawn(self, entry: _Entry) -> None:
        """为登记项拉起循环任务；任务仍在运行时不重复拉起。"""
        if entry.task is not None and not entry.task.done():
            return
        generation = self._run_generation
        task = asyncio.ensure_future(self._loop(entry, generation))
        entry.task = task
        task.add_done_callback(
            lambda _done, _entry=entry, _task=task, _gen=generation: (
                self._on_loop_done(_entry, _task, _gen)
            )
        )

    def _on_loop_done(self, entry: _Entry, task: asyncio.Task, generation: int) -> None:
        """循环收尾接力：stop 期间并发 start 的潜伏项在此被重新拉起。

        循环因代次不符退出时，若调度器已由更新的代次重新 start 且该
        entry 当前登记的正是这个已结束任务，说明 stop 与 start 交错
        时 _spawn 曾因旧任务未结束而跳过——以当前代次补拉一次。单次
        任务（代次未变、自然结束）与关停态（未 started）不接力。
        """
        if not self._started or entry.task is not task:
            return
        if generation == self._run_generation:
            return
        self._spawn(entry)

    async def _loop(self, entry: _Entry, generation: int) -> None:
        """单个任务的运行循环：跑 runner、按延迟等待、再跑。

        每轮在 runner 返回/异常处理后、再次调用 runner 之前都校验
        代次：stop 已推进代次时，哪怕 runner 吞掉了 CancelledError
        并正常返回，本循环也到此退出，绝不再次调用 runner。
        """
        while True:
            if generation != self._run_generation:
                return
            try:
                delay = await entry.runner()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 循环不得被 runner 异常杀死
                entry.runs += 1
                entry.last_error = type(exc).__name__
                if generation != self._run_generation:
                    return
                delay = entry.interval if entry.interval is not None else 1.0
                await self._sleep(delay)
                continue
            entry.runs += 1
            if generation != self._run_generation:
                return
            if delay is None:
                delay = entry.interval
            if delay is None:
                # runner 无延迟且无登记周期：只跑一次便结束。
                return
            await self._sleep(delay)


@dataclass
class _PendingCare:
    """关怀待发项：入队时刻的 epoch 与 generation 戳记随项保存。"""

    user_id: str
    work_id: str
    created_at: datetime
    due_at: datetime
    epoch: int
    generation: int | None


class CareScheduler:
    """主动关怀待发队列与到期触发发送（鸭子类型，不依赖具体层）。

    gate 只要求带 ``async def send(user_id, work_id, created_at) -> bool``；
    若 gate 的 send 另接受 ``fence`` 关键字（CareSendGate 即如此），
    触发时传入发送中复核闭包（E 路修复 3）：gate 在其内部 await
    （如安全 detect）返回后、真正发送前求值，epoch/代次任一已变即
    放弃——补上「run_due 校验通过后、gate 内 await 期间被撤回/换代」
    的窗口。不接受 fence 的 gate 按原三参契约调用，行为不变。
    generation_of 为可选 async callable，按 user_id 返回当前投递代次。
    发送一律一去不回：到期项无论发送成功、被拒（False）还是抛异常
    都被消费、永不重试（异常只记类型名到 last_error，不记正文）。
    """

    def __init__(self, gate, *, clock=None, generation_of=None) -> None:
        """绑定发送门与时钟；clock 返回 aware datetime，默认 UTC 现在。"""
        self._gate = gate
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._generation_of = generation_of
        self._epochs: dict[str, int] = {}
        self._pending: list[_PendingCare] = []
        self.last_error: str | None = None

    def enqueue(
        self,
        user_id: str,
        work_id: str,
        created_at: datetime,
        *,
        due_at: datetime | None = None,
        generation: int | None = None,
    ) -> bool:
        """把一条关怀工作排入待发队列，返回是否真正入队。

        due_at 缺省等于 created_at。同 (user_id, work_id) 已在队时
        返回 False 且不重复入队。入队时戳记该用户当前 epoch 与
        传入的 generation，供触发时校验是否已被撤回/换代。注意：
        构造时注入了 generation_of 的实例在触发时要求 generation
        戳记非空，缺戳记的项会被直接丢弃（fail closed）。
        """
        if any(
            item.user_id == user_id and item.work_id == work_id
            for item in self._pending
        ):
            return False
        self._pending.append(
            _PendingCare(
                user_id=user_id,
                work_id=work_id,
                created_at=created_at,
                due_at=due_at if due_at is not None else created_at,
                epoch=self._epochs.get(user_id, 0),
                generation=generation,
            )
        )
        return True

    def invalidate(self, user_id: str) -> int:
        """撤回某用户的全部待发项并推进其 epoch，返回删除的项数。

        epoch 推进保证：即使某项已在触发途中被单独取出，事后新入队
        的项也不会与撤回前的旧戳记混淆。正在 gate.send 中阻塞的项
        已在发送前被消费，不在此列（其发送结果按 gate 口径记账）。
        """
        before = len(self._pending)
        self._pending = [
            item for item in self._pending if item.user_id != user_id
        ]
        self._epochs[user_id] = self._epochs.get(user_id, 0) + 1
        return before - len(self._pending)

    def pending_count(self, user_id: str | None = None) -> int:
        """返回待发项数；给出 user_id 时只统计该用户的待发项。"""
        if user_id is None:
            return len(self._pending)
        return sum(1 for item in self._pending if item.user_id == user_id)

    async def run_due(self, now: datetime | None = None) -> int:
        """触发全部到期项发送，返回本次实际调用 gate.send 的次数。

        now 缺省取 clock()。到期项按 due_at 升序处理：epoch 戳记与
        该用户当前 epoch 不符、或 generation 戳记与当前代次不符的
        项直接丢弃、不发送；其余项在发送前先出队消费，再调用
        gate.send——返回 False 或抛异常都不重试。未到期项保留。
        """
        moment = now if now is not None else self._clock()
        due = sorted(
            (item for item in self._pending if item.due_at <= moment),
            key=lambda item: item.due_at,
        )
        sent = 0
        for item in due:
            if not self._still_pending(item):
                # 已被并发的 invalidate / run_due 处理掉。
                continue
            if item.epoch != self._epochs.get(item.user_id, 0):
                self._discard(item)
                continue
            if self._generation_of is not None:
                if item.generation is None:
                    # 已接线代次校验时缺戳记 = 无法证明该项未跨代，
                    # fail closed 直接丢弃（Review P3-1：不允许入队方
                    # 漏传 generation 静默退化为「新消息不失效关怀」）。
                    self._discard(item)
                    continue
                try:
                    current_generation = await self._generation_of(item.user_id)
                except Exception as exc:  # noqa: BLE001 — 代次查不到则不发送
                    self.last_error = type(exc).__name__
                    self._discard(item)
                    continue
                # generation_of 的 await 期间可能发生撤回：发送前复核。
                if not self._still_pending(item):
                    continue
                if item.epoch != self._epochs.get(item.user_id, 0):
                    self._discard(item)
                    continue
                if current_generation != item.generation:
                    self._discard(item)
                    continue
            # 发送前先消费：无论结果如何都永不重试，也防并发重发。
            self._discard(item)
            sent += 1
            try:
                if self._gate_accepts_fence():
                    await self._gate.send(
                        item.user_id,
                        item.work_id,
                        item.created_at,
                        fence=self._make_fence(item),
                    )
                else:
                    await self._gate.send(item.user_id, item.work_id, item.created_at)
            except Exception as exc:  # noqa: BLE001 — 发送异常吞掉，只记类型名
                self.last_error = type(exc).__name__
        return sent

    def _gate_accepts_fence(self) -> bool:
        """gate.send 是否接受 fence 关键字（形参名命中或带 **kwargs）。"""
        try:
            params = inspect.signature(self._gate.send).parameters
        except (TypeError, ValueError):
            return False
        return "fence" in params or any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
        )

    def _make_fence(self, item: _PendingCare):
        """构造发送中复核闭包：求值时刻的 epoch 与投递代次须仍与戳记一致。

        代次读取口径与 run_due 的触发前校验相同（构造注入的
        generation_of，生产接线为 ledger.delivery_generation）。
        任一读取失败按不通过处理（fail closed）。
        """
        scheduler = self

        async def fence() -> bool:
            if item.epoch != scheduler._epochs.get(item.user_id, 0):
                return False
            if scheduler._generation_of is not None:
                if item.generation is None:
                    return False
                try:
                    current = await scheduler._generation_of(item.user_id)
                except Exception:  # noqa: BLE001 — 代次查不到则不发送
                    return False
                if current != item.generation:
                    return False
                # generation_of 的 await 期间可能发生撤回：再核 epoch。
                if item.epoch != scheduler._epochs.get(item.user_id, 0):
                    return False
            return True

        return fence

    async def tick(self, interval: float) -> float:
        """跑一轮到期触发并返回 float(interval)，供周期注册到调度器。"""
        await self.run_due()
        return float(interval)

    def _still_pending(self, item: _PendingCare) -> bool:
        """按对象身份判断待发项是否仍在队列中。"""
        return any(candidate is item for candidate in self._pending)

    def _discard(self, item: _PendingCare) -> None:
        """按对象身份从队列中移除待发项（不存在时无副作用）。"""
        self._pending = [
            candidate for candidate in self._pending if candidate is not item
        ]
