"""文件域异步任务生命周期契约（方案 C18 补强，N5）。

``FileTaskManager`` 为文件上传/下载类动作提供统一的任务账：并发
上限（信号量排队）、超时、取消、临时文件登记与配额、终态结果码。
本模块只定义契约与进程内实现，不做任何真实网络/QQ 调用——实际
传输由调用方以 ``factory`` / ``send_chunk`` 回调注入。

终态口径（终态不可逆，先到终态为准，后续迁移一律不覆盖）：
- 终态 = succeeded / failed / cancelled / timed_out。
- 对终态任务再调 ``run`` / ``stream_upload`` → ``FileTaskError``。
- 对终态任务调 ``cancel`` → 忽略，返回 False（不抛错）。
- ``cleanup`` 幂等：终态/重复调用均可，只删文件、清配额记账，
  不改状态。

错误留痕纪律：``FileTask.error`` 与失败 ``result_code`` 只记异常
类型名，绝不记异常正文（正文可能含路径/签名 URL）。
"""

import asyncio
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class FileTaskError(ValueError):
    """文件任务契约违例（终态重入、临时路径越界、配额超额等）。"""


class FileTaskState(str, Enum):
    """文件任务状态机。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATES = frozenset(
    {
        FileTaskState.SUCCEEDED,
        FileTaskState.FAILED,
        FileTaskState.CANCELLED,
        FileTaskState.TIMED_OUT,
    }
)

# 兼容字符串常量别名。
STATE_PENDING = FileTaskState.PENDING.value
STATE_RUNNING = FileTaskState.RUNNING.value
STATE_SUCCEEDED = FileTaskState.SUCCEEDED.value
STATE_FAILED = FileTaskState.FAILED.value
STATE_CANCELLED = FileTaskState.CANCELLED.value
STATE_TIMED_OUT = FileTaskState.TIMED_OUT.value


@dataclass
class FileTask:
    """单个文件任务的账面（可变，由 Manager 迁移状态）。

    ``progress`` 为 0..1，未知总数时为 None；``sent_chunks`` /
    ``sent_bytes`` 为流式上传的累计计数（总数未知时以此为准）。
    ``error`` 只记异常类型名。``created_at`` / ``updated_at`` 与
    Manager 的 ``clock`` 同口径（默认 ``time.monotonic``）。
    """

    task_id: str
    action: str
    state: FileTaskState = FileTaskState.PENDING
    progress: float | None = None
    result_code: str | None = None
    resource_digest: str = ""
    sensitivity: str = "normal"
    #: repr=False：真实临时路径不经默认 repr 进日志（Review P3-6）。
    temp_paths: tuple = field(default=(), repr=False)
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    sent_chunks: int = 0
    sent_bytes: int = 0
    total_chunks: int | None = None
    result: object = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class FileTaskManager:
    """文件任务管理器（单事件循环假设，无跨线程锁）。

    构造参数：``max_concurrent`` 并发上限（``run`` 与
    ``stream_upload`` 共用同一信号量排队，超出者等待）；``temp_root``
    临时文件根（None 时以系统临时目录为准）；``max_temp_bytes``
    全部任务临时文件总配额（None 不限，登记时按文件实际大小累计）；
    ``clock`` 时钟（默认 ``time.monotonic``）。
    """

    def __init__(
        self, *, max_concurrent=4, temp_root=None, max_temp_bytes=None, clock=time.monotonic
    ):
        if not isinstance(max_concurrent, int) or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive int")
        if max_temp_bytes is not None and max_temp_bytes < 0:
            raise ValueError("max_temp_bytes must be non-negative or None")
        self.max_concurrent = max_concurrent
        self.temp_root = str(temp_root) if temp_root is not None else None
        self.max_temp_bytes = max_temp_bytes
        self.clock = clock
        self._tasks: dict[str, FileTask] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._temp_sizes: dict[str, int] = {}
        self._temp_refcounts: dict[str, int] = {}
        self._temp_total = 0
        self._semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop = None

    # -- 基础账 ---------------------------------------------------------

    def _now(self) -> float:
        return float(self.clock())

    def _touch(self, task: FileTask) -> None:
        task.updated_at = self._now()

    def create(self, action, *, resource_digest="", sensitivity="normal") -> FileTask:
        """新建 pending 任务并入账。"""
        if not isinstance(action, str) or not action:
            raise FileTaskError("action must be a non-empty string")
        now = self._now()
        task = FileTask(
            task_id=uuid.uuid4().hex,
            action=action,
            resource_digest=str(resource_digest or ""),
            sensitivity=str(sensitivity or "normal"),
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id) -> FileTask | None:
        return self._tasks.get(task_id)

    def _require(self, task_id) -> FileTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise FileTaskError(f"unknown task: {task_id!r}")
        return task

    def _get_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
            self._semaphore_loop = loop
        return self._semaphore

    def _set_terminal(
        self, task: FileTask, state: FileTaskState, *, result_code, error=None
    ) -> bool:
        """迁到终态；已是终态则保持先到终态，返回 False。"""
        if task.state in TERMINAL_STATES:
            return False
        task.state = state
        task.result_code = result_code
        task.error = error
        self._touch(task)
        return True

    # -- 执行 -----------------------------------------------------------

    async def run(self, task: FileTask, factory, *, timeout=None):
        """执行一个 pending 任务并返回 factory 的结果。

        ``factory`` 为无参 callable，调用后得到 coroutine。并发超
        上限时先排队（等待期间仍为 pending）；拿到信号量后置
        running 再执行。成功 → succeeded（result_code="ok"）并返回
        结果；factory 抛普通异常 → failed（result_code/error 落
        异常类型名）并清理临时文件后原样上抛；``timeout`` 到期
        （``asyncio.wait_for``）→ timed_out 后上抛 TimeoutError；
        被取消（含 ``cancel()``）→ cancelled + 清理后上抛
        CancelledError——取消后 factory 的 coroutine 不会被继续
        推进（wait_for/await 的取消语义保证）。终态任务再 run
        抛 ``FileTaskError``；running 任务重入同样抛错。
        """
        if not isinstance(task, FileTask):
            raise FileTaskError("task must be a FileTask")
        if task.state in TERMINAL_STATES:
            raise FileTaskError(f"task already terminal: {task.state.value}")
        if task.state == FileTaskState.RUNNING:
            raise FileTaskError("task already running")
        self._tasks.setdefault(task.task_id, task)
        semaphore = self._get_semaphore()
        current = asyncio.current_task()
        if current is not None:
            self._running[task.task_id] = current
        try:
            async with semaphore:
                if task.state in TERMINAL_STATES:
                    # 排队期间被 cancel() 直接终结：不得再执行 factory。
                    raise FileTaskError(f"task already terminal: {task.state.value}")
                if task.state != FileTaskState.PENDING:
                    raise FileTaskError("task already running")
                task.state = FileTaskState.RUNNING
                self._touch(task)
                try:
                    coro = factory()
                    if timeout is not None:
                        result = await asyncio.wait_for(coro, timeout)
                    else:
                        result = await coro
                except asyncio.CancelledError:
                    if self._set_terminal(
                        task, FileTaskState.CANCELLED, result_code="cancelled"
                    ):
                        self.cleanup(task)
                    raise
                except (asyncio.TimeoutError, TimeoutError):
                    if timeout is None:
                        # 与 stream_upload 同口径（复审 P4）：无截止
                        # 配置时 TimeoutError 来自 factory 自身，按
                        # 一般失败落账，不冒充 deadline 超时。
                        if self._set_terminal(
                            task,
                            FileTaskState.FAILED,
                            result_code="TimeoutError",
                            error="TimeoutError",
                        ):
                            self.cleanup(task)
                        raise
                    if self._set_terminal(
                        task,
                        FileTaskState.TIMED_OUT,
                        result_code="timeout",
                        error="TimeoutError",
                    ):
                        self.cleanup(task)
                    raise
                except Exception as exc:  # noqa: BLE001 — 类型名落账后原样上抛
                    if self._set_terminal(
                        task,
                        FileTaskState.FAILED,
                        result_code=type(exc).__name__,
                        error=type(exc).__name__,
                    ):
                        self.cleanup(task)
                    raise
                task.result = result
                if task.progress is None:
                    task.progress = 1.0
                self._set_terminal(task, FileTaskState.SUCCEEDED, result_code="ok")
                return result
        except asyncio.CancelledError:
            # 在信号量排队等待期间被取消：尚未 running，同样落 cancelled。
            if task.state == FileTaskState.PENDING:
                if self._set_terminal(task, FileTaskState.CANCELLED, result_code="cancelled"):
                    self.cleanup(task)
            raise
        finally:
            if current is not None and self._running.get(task.task_id) is current:
                self._running.pop(task.task_id, None)

    def cancel(self, task_id) -> bool:
        """取消任务：取消承载 run/stream_upload 的 asyncio.Task。

        终态或未知任务忽略并返回 False；实际受理返回 True。状态
        落账分工（Review P3-1 文档化）：已登记执行者的任务（含
        信号量排队中——排队等待点同样被取消、factory 永不执行）
        由 ``run``/``stream_upload`` 的 CancelledError 处理落
        cancelled；无执行者的 pending 任务由本方法直接落终态。
        注意被取消的 asyncio.Task 是调用方承载 run 的那个任务
        本身：调用方若不希望自身的其他工作被一并取消，应以
        ``asyncio.create_task`` 独立包装 run/stream_upload。
        """
        task = self._tasks.get(task_id)
        if task is None or task.state in TERMINAL_STATES:
            return False
        running = self._running.get(task_id)
        if running is not None and not running.done():
            running.cancel()
            return True
        if self._set_terminal(task, FileTaskState.CANCELLED, result_code="cancelled"):
            self.cleanup(task)
        return True

    # -- 临时文件 ---------------------------------------------------------

    def _temp_allowed_roots(self) -> list[str]:
        roots = []
        if self.temp_root is not None:
            roots.append(os.path.realpath(self.temp_root))
        roots.append(os.path.realpath(tempfile.gettempdir()))
        return roots

    def register_temp_path(self, task_id, path) -> FileTask:
        """登记任务的临时文件（须在 temp_root 内或系统临时目录）。

        越界抛 ``FileTaskError``；文件存在时按实际大小累计配额，
        超 ``max_temp_bytes`` 拒绝登记（不入账）。同一路径重复登记
        幂等（不重复计配额）。返回任务本身。
        """
        task = self._require(task_id)
        real = os.path.realpath(str(path))
        roots = self._temp_allowed_roots()
        if not any(real == root or real.startswith(root + os.sep) for root in roots):
            raise FileTaskError("temp path outside temp roots")
        if real in task.temp_paths:
            return task
        # 跨任务同路径引用计数（Review P3-2）：同一 realpath 被多个
        # 任务登记时配额只记一份、文件只在最后一个引用 cleanup 时
        # 删除——此前覆盖写 + 重复加总会令配额永久残留、且先 cleanup
        # 的任务会删掉他人在用的文件。
        if real in self._temp_sizes:
            self._temp_refcounts[real] = self._temp_refcounts.get(real, 1) + 1
            task.temp_paths = (*task.temp_paths, real)
            self._touch(task)
            return task
        size = os.path.getsize(real) if os.path.isfile(real) else 0
        if self.max_temp_bytes is not None and self._temp_total + size > self.max_temp_bytes:
            raise FileTaskError("temp quota exceeded")
        task.temp_paths = (*task.temp_paths, real)
        self._temp_sizes[real] = size
        self._temp_refcounts[real] = 1
        self._temp_total += size
        self._touch(task)
        return task

    def cleanup(self, task) -> int:
        """删除任务已登记的临时文件并清配额记账；返回实际删除数。

        文件不存在不报错；幂等；不改变任务状态。``task`` 可为
        FileTask 或 task_id。
        """
        resolved = self._tasks.get(task) if isinstance(task, str) else task
        if resolved is None:
            return 0
        removed = 0
        for path in resolved.temp_paths:
            remaining = self._temp_refcounts.get(path, 1) - 1
            if remaining > 0:
                # 仍有其他任务引用：保留文件与配额记账（P3-2）。
                self._temp_refcounts[path] = remaining
                continue
            self._temp_refcounts.pop(path, None)
            size = self._temp_sizes.pop(path, 0)
            self._temp_total = max(0, self._temp_total - size)
            try:
                os.remove(path)
                removed += 1
            except FileNotFoundError:
                pass
            except IsADirectoryError:
                pass
            except OSError:
                pass
        resolved.temp_paths = ()
        self._touch(resolved)
        return removed

    @property
    def temp_bytes_used(self) -> int:
        """当前全部任务已登记临时文件的配额占用（字节）。"""
        return self._temp_total

    # -- 流式上传 ---------------------------------------------------------

    async def stream_upload(self, task: FileTask, chunks, send_chunk, *, timeout=None):
        """逐块流式发送并维护进度，成功返回任务本身。

        ``chunks`` 为 sync 或 async 可迭代的 ``(index, bytes)``
        序列；``send_chunk(index, data)`` 为 async callable。每块
        成功后更新：总数已知（chunks 有 ``len``）时
        ``progress = 已发 / 总数``，未知时 progress 保持 None、只
        累计 ``sent_chunks`` / ``sent_bytes``。pending 任务开始时
        置 running；全部发完 → succeeded、progress=1.0。

        并发口径与 ``run`` 一致（补审第二段 P3）：发送循环在
        ``max_concurrent`` 信号量内执行，超上限者排队（等待期间
        仍为 pending），拿到信号量后复检终态才置 running；
        ``timeout`` 口径同 ``run``（到期 → timed_out 后上抛
        TimeoutError）。

        取消（含 ``cancel()``）在当前 await 点以 CancelledError
        中断：落 cancelled + 清理后上抛，且绝不再调 ``send_chunk``
        （循环已退出，后续块不会被取用）。``send_chunk`` 或块迭代
        抛普通异常 → failed，result_code/error 落异常类型名，清理
        后原样上抛。终态任务调用抛 ``FileTaskError``。
        """
        if not isinstance(task, FileTask):
            raise FileTaskError("task must be a FileTask")
        if task.state in TERMINAL_STATES:
            raise FileTaskError(f"task already terminal: {task.state.value}")
        if task.state == FileTaskState.RUNNING:
            # 重入保护（Review P3-3，与 run() 对齐）：同一任务不得
            # 被两个协程同时流式发送——双循环会并行调 send_chunk、
            # 计数互相覆盖、cancel 只能取消其一。
            raise FileTaskError("task already running")
        self._tasks.setdefault(task.task_id, task)
        semaphore = self._get_semaphore()
        current = asyncio.current_task()
        if current is not None:
            self._running[task.task_id] = current
        try:
            async with semaphore:
                if task.state in TERMINAL_STATES:
                    # 排队期间被 cancel() 直接终结：不得再发送。
                    raise FileTaskError(f"task already terminal: {task.state.value}")
                if task.state != FileTaskState.PENDING:
                    raise FileTaskError("task already running")
                task.state = FileTaskState.RUNNING
                self._touch(task)
                try:
                    coro = self._stream_send(task, chunks, send_chunk)
                    if timeout is not None:
                        await asyncio.wait_for(coro, timeout)
                    else:
                        await coro
                except asyncio.CancelledError:
                    if self._set_terminal(
                        task, FileTaskState.CANCELLED, result_code="cancelled"
                    ):
                        self.cleanup(task)
                    raise
                except (asyncio.TimeoutError, TimeoutError):
                    if timeout is None:
                        # 无截止配置在场时 TimeoutError 只可能来自
                        # send_chunk 自身（复审 P4）：按一般失败
                        # 落账，不得冒充 deadline 超时误导分诊。
                        if self._set_terminal(
                            task,
                            FileTaskState.FAILED,
                            result_code="TimeoutError",
                            error="TimeoutError",
                        ):
                            self.cleanup(task)
                        raise
                    if self._set_terminal(
                        task,
                        FileTaskState.TIMED_OUT,
                        result_code="timeout",
                        error="TimeoutError",
                    ):
                        self.cleanup(task)
                    raise
                except Exception as exc:  # noqa: BLE001 — 类型名落账后原样上抛
                    if self._set_terminal(
                        task,
                        FileTaskState.FAILED,
                        result_code=type(exc).__name__,
                        error=type(exc).__name__,
                    ):
                        self.cleanup(task)
                    raise
                return task
        except asyncio.CancelledError:
            # 在信号量排队等待期间被取消：尚未 running，同样落
            # cancelled（与 run() 同口径）。
            if task.state == FileTaskState.PENDING:
                if self._set_terminal(task, FileTaskState.CANCELLED, result_code="cancelled"):
                    self.cleanup(task)
            raise
        finally:
            if current is not None and self._running.get(task.task_id) is current:
                self._running.pop(task.task_id, None)

    async def _stream_send(self, task: FileTask, chunks, send_chunk) -> None:
        """stream_upload 的发送循环本体：信号量与终态收口在调用方。

        逐块发送并维护进度/计数；全部发完落 succeeded。异常不在
        此收口（由 stream_upload 按取消/超时/失败分类落终态）。
        """
        total = None
        try:
            total = len(chunks)  # type: ignore[arg-type]
        except TypeError:
            total = None
        task.total_chunks = total
        iterator = _aiter_chunks(chunks)
        while True:
            if task.state == FileTaskState.CANCELLED:
                raise asyncio.CancelledError()
            try:
                index, data = await iterator.__anext__()
            except StopAsyncIteration:
                break
            await send_chunk(index, data)
            task.sent_chunks += 1
            task.sent_bytes += len(data) if data is not None else 0
            if total:
                task.progress = min(1.0, task.sent_chunks / total)
            self._touch(task)
        task.progress = 1.0
        self._set_terminal(task, FileTaskState.SUCCEEDED, result_code="ok")


async def _aiter_chunks(chunks):
    """把 sync / async 可迭代统一为 async 迭代器（逐项产出）。"""
    if hasattr(chunks, "__aiter__"):
        async for item in chunks:
            yield item
        return
    for item in chunks:
        yield item
