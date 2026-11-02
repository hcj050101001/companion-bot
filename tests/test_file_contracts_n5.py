"""N5 文件契约单元测试（方案 C18）：resources / file_tasks / circuit。

只测本阶段三个契约模块：ResourceResolver 完整版（含
validate_resource_ref 的 N1 冻结语义回归）、FileTaskManager 异步
任务生命周期、ActionCircuitBreaker。无真实网络/QQ；临时文件只用
pytest tmp_path；异步用 asyncio.run（仓库无 pytest-asyncio）。
"""

import asyncio
import hashlib
import os
import time

import pytest

from companion.adapters.circuit import ActionCircuitBreaker
from companion.adapters.file_tasks import (
    FileTaskError,
    FileTaskManager,
    FileTaskState,
)
from companion.adapters.resources import (
    _DEFAULT_IGNORABLE_RANGES,
    ResourceKind,
    ResourceResolver,
    ResourceValidationError,
    _contains_default_ignorable,
    _normalize_whatwg_host,
    validate_download_url,
    validate_on_disk_name,
    validate_path_semantic_ref,
    validate_resource_ref,
)
from tests.uts46_ignored_snapshot import UTS46_IGNORED_CODEPOINTS

pytestmark = pytest.mark.timeout(10)


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ------------------------------------------------------------ resolver 分类


def test_resolver_url_kind_digest_and_ext():
    resolver = ResourceResolver()
    resolved = resolver.resolve("https://example.com/pic.JPG?x=1")
    assert resolved.kind == ResourceKind.URL
    assert resolved.kind == "url"
    assert resolved.ref == "https://example.com/pic.JPG?x=1"
    expected = hashlib.sha256(resolved.ref.encode("utf-8")).hexdigest()[:16]
    assert resolved.digest == expected
    assert len(resolved.digest) == 16
    assert resolved.media_ext == "jpg"
    assert resolved.sensitivity == "normal"
    assert resolved.size_hint is None
    assert resolved.bound_self_id is None and resolved.bound_session is None
    assert resolved.expires_at is None


def test_resolver_url_uppercase_scheme():
    resolved = ResourceResolver().resolve("HTTPS://example.com/f.txt")
    assert resolved.kind == ResourceKind.URL
    assert resolved.media_ext == "txt"


@pytest.mark.parametrize(
    "ref",
    [
        "https://cdn.example.com/a?sign=abc123",
        "https://cdn.example.com/a?token=abc123",
        "https://cdn.example.com/a?key=abc123",
        "https://cdn.example.com/a?access_token=abc123",
        "https://cdn.example.com/a?x=1&api_key=abc123",
        "https://cdn.example.com/a?X-Amz-Signature=abc123",
    ],
)
def test_resolver_signed_url_marked(ref):
    resolved = ResourceResolver().resolve(ref)
    assert resolved.sensitivity == "signed_url"
    # digest 不落原文：digest 本身不含签名值。
    assert "abc123" not in resolved.digest


def test_resolver_base64_kind_and_estimate():
    resolver = ResourceResolver()
    resolved = resolver.resolve("base64://QUJDRA==")  # 8 chars -> est. 6 bytes
    assert resolved.kind == ResourceKind.BASE64
    assert resolved.size_hint == 6
    assert resolved.media_ext is None
    explicit = resolver.resolve("base64://QUJDRA==", size_hint=4)
    assert explicit.size_hint == 4


def test_resolver_base64_over_max_bytes_rejected():
    resolver = ResourceResolver(max_bytes=10)
    with pytest.raises(ResourceValidationError):
        resolver.resolve("base64://" + "A" * 100)  # est. 75 bytes > 10
    with pytest.raises(ResourceValidationError):
        resolver.resolve("https://example.com/big.bin", size_hint=11)


def test_resolver_base64_empty_payload_rejected():
    with pytest.raises(ResourceValidationError):
        ResourceResolver().resolve("base64://")


def test_resolver_local_path_inside_roots(tmp_path):
    target = tmp_path / "a.txt"
    target.write_bytes(b"hi")
    resolver = ResourceResolver(allowed_roots=(tmp_path,))
    resolved = resolver.resolve(str(target))
    assert resolved.kind == ResourceKind.LOCAL_PATH
    assert resolved.ref == str(target)
    assert resolved.media_ext == "txt"


def test_resolver_file_url_form(tmp_path):
    target = tmp_path / "b.bin"
    resolver = ResourceResolver(allowed_roots=(tmp_path,))
    resolved = resolver.resolve("file://" + str(target))
    assert resolved.kind == ResourceKind.LOCAL_PATH
    assert resolved.media_ext == "bin"
    with pytest.raises(ResourceValidationError):
        resolver.resolve("file:///etc/passwd")


def test_resolver_local_path_outside_roots_rejected(tmp_path):
    resolver = ResourceResolver(allowed_roots=(tmp_path,))
    with pytest.raises(ResourceValidationError):
        resolver.resolve("/etc/passwd")
    with pytest.raises(ResourceValidationError):
        ResourceResolver().resolve(str(tmp_path / "a.txt"))  # roots 空全拒


def test_resolver_dotdot_traversal_rejected(tmp_path):
    resolver = ResourceResolver(allowed_roots=(tmp_path,))
    escape = str(tmp_path / ".." / "outside.txt")
    with pytest.raises(ResourceValidationError):
        resolver.resolve(escape)


def test_resolver_symlink_traversal_rejected(tmp_path):
    outside = tmp_path.parent / "n5_outside_secret.txt"
    outside.write_bytes(b"x")
    link = tmp_path / "link.txt"
    os.symlink(outside, link)
    resolver = ResourceResolver(allowed_roots=(tmp_path,))
    with pytest.raises(ResourceValidationError):
        resolver.resolve(str(link))


def test_resolver_allowed_exts_whitelist(tmp_path):
    resolver = ResourceResolver(allowed_roots=(tmp_path,), allowed_exts={"txt", ".md"})
    assert resolver.resolve(str(tmp_path / "a.txt")).media_ext == "txt"
    assert resolver.resolve(str(tmp_path / "a.MD")).media_ext == "md"
    with pytest.raises(ResourceValidationError):
        resolver.resolve(str(tmp_path / "a.png"))
    with pytest.raises(ResourceValidationError):
        resolver.resolve(str(tmp_path / "noext"))


def test_resolver_file_id_passthrough():
    resolver = ResourceResolver()
    resolved = resolver.resolve("plain-file-id-123")
    assert resolved.kind == ResourceKind.FILE_ID
    assert resolved.ref == "plain-file-id-123"
    # 相对路径不是绝对路径，按不透明 file_id 放行（与旧校验同口径）。
    assert resolver.resolve("data/a.txt").kind == ResourceKind.FILE_ID


@pytest.mark.parametrize(
    "bad",
    [None, 123, b"bytes", "", "   ", "ftp://example.com/x", "gopher://x", "ws://x/y"],
)
def test_resolver_rejects_bad_refs(bad):
    with pytest.raises(ResourceValidationError):
        ResourceResolver().resolve(bad)


# ------------------------------------------------------------ resolver 绑定


def test_resolver_binding_match_and_mismatch():
    clock = FakeClock()
    resolver = ResourceResolver(clock=clock, binding_ttl=60)
    resolved = resolver.resolve("https://example.com/a", self_id="100", session="s1")
    assert resolved.bound_self_id == "100"
    assert resolved.bound_session == "s1"
    assert resolved.expires_at == 1060.0
    resolver.check_binding(resolved, self_id="100", session="s1")
    resolver.check_binding(resolved, self_id=100, session="s1")  # 数字 id 同口径
    with pytest.raises(ResourceValidationError):
        resolver.check_binding(resolved, self_id="200", session="s1")
    with pytest.raises(ResourceValidationError):
        resolver.check_binding(resolved, self_id="100", session="other")
    with pytest.raises(ResourceValidationError):
        resolver.check_binding(resolved, self_id=None, session="s1")


def test_resolver_binding_expires():
    clock = FakeClock()
    resolver = ResourceResolver(clock=clock, binding_ttl=30)
    resolved = resolver.resolve("file-id-1", self_id="100")
    resolver.check_binding(resolved, self_id="100")
    clock.advance(31)
    with pytest.raises(ResourceValidationError):
        resolver.check_binding(resolved, self_id="100")


def test_resolver_unbound_fields_not_checked():
    clock = FakeClock()
    resolver = ResourceResolver(clock=clock, binding_ttl=30)
    resolved = resolver.resolve("file-id-1")
    assert resolved.expires_at is None  # 无绑定 + ttl 不产生过期时刻
    resolver.check_binding(resolved, self_id="anyone", session="anything")
    resolver.check_binding(resolved)
    only_self = resolver.resolve("file-id-2", self_id="100")
    resolver.check_binding(only_self, self_id="100", session="whatever")
    with pytest.raises(ResourceValidationError):
        resolver.check_binding(only_self, self_id="101", session="whatever")


# ------------------------------------------------- validate_resource_ref 回归


def test_validate_resource_ref_frozen_semantics(tmp_path):
    # N1 旧用例逐条回归（test_gateway_n1.test_resource_ref_rules）。
    with pytest.raises(ResourceValidationError):
        validate_resource_ref("file:///etc/passwd")
    with pytest.raises(ResourceValidationError):
        validate_resource_ref("/etc/passwd", allowed_roots=(tmp_path,))
    with pytest.raises(ResourceValidationError):
        validate_resource_ref("/etc/passwd")
    inside = str(tmp_path / "a.txt")
    assert validate_resource_ref(inside, allowed_roots=(tmp_path,)) == inside
    assert validate_resource_ref("HTTPS://example.com/f?x=1") == "HTTPS://example.com/f?x=1"
    assert validate_resource_ref("plain-file-id-123") == "plain-file-id-123"
    # resolver 放行的两类在旧函数口径下仍拒绝（网关递归校验依赖此点）。
    with pytest.raises(ResourceValidationError):
        validate_resource_ref("base64://QUJD")
    with pytest.raises(ResourceValidationError):
        validate_resource_ref(123)


# ------------------------------------------------------------ file tasks


def test_task_create_defaults():
    clock = FakeClock()
    manager = FileTaskManager(clock=clock)
    task = manager.create("upload_file", resource_digest="abc123", sensitivity="signed_url")
    assert len(task.task_id) == 32
    assert task.action == "upload_file"
    assert task.state == FileTaskState.PENDING
    assert task.state == "pending"
    assert task.progress is None and task.result_code is None and task.error is None
    assert task.temp_paths == ()
    assert task.resource_digest == "abc123" and task.sensitivity == "signed_url"
    assert task.created_at == 1000.0 and task.updated_at == 1000.0
    assert manager.get(task.task_id) is task


def test_task_run_success():
    manager = FileTaskManager()

    async def main():
        task = manager.create("get_file")

        async def factory_impl():
            return {"ok": True}

        result = await manager.run(task, lambda: factory_impl())
        assert result == {"ok": True}
        return task

    task = run(main())
    assert task.state == FileTaskState.SUCCEEDED
    assert task.result_code == "ok"
    assert task.progress == 1.0
    assert task.result == {"ok": True}


def test_task_run_failure_records_type_name_only():
    manager = FileTaskManager()

    async def main():
        task = manager.create("get_file")

        async def factory_impl():
            raise RuntimeError("sensitive body must not be recorded")

        with pytest.raises(RuntimeError):
            await manager.run(task, lambda: factory_impl())
        return task

    task = run(main())
    assert task.state == FileTaskState.FAILED
    assert task.result_code == "RuntimeError"
    assert task.error == "RuntimeError"
    assert "sensitive" not in (task.error or "") + (task.result_code or "")


def test_task_run_timeout():
    manager = FileTaskManager()

    async def main():
        task = manager.create("download_file")

        async def factory_impl():
            await asyncio.sleep(10)

        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await manager.run(task, lambda: factory_impl(), timeout=0.05)
        return task

    task = run(main())
    assert task.state == FileTaskState.TIMED_OUT
    assert task.state == "timed_out"
    assert task.result_code == "timeout"
    assert task.error == "TimeoutError"


def test_task_run_cancel_has_no_late_side_effects(tmp_path):
    manager = FileTaskManager(temp_root=tmp_path)
    side_effects = {"before": 0, "after": 0}

    async def main():
        task = manager.create("upload_file")
        temp_file = tmp_path / "chunk.tmp"
        temp_file.write_bytes(b"123")
        manager.register_temp_path(task.task_id, temp_file)

        async def factory_impl():
            side_effects["before"] += 1
            await asyncio.sleep(10)
            side_effects["after"] += 1  # 取消后绝不应执行到
            return None

        runner = asyncio.create_task(manager.run(task, lambda: factory_impl()))
        await asyncio.sleep(0.05)
        assert manager.cancel(task.task_id) is True
        with pytest.raises(asyncio.CancelledError):
            await runner
        await asyncio.sleep(0.05)
        return task, temp_file

    task, temp_file = run(main())
    assert task.state == FileTaskState.CANCELLED
    assert task.result_code == "cancelled"
    assert side_effects == {"before": 1, "after": 0}
    assert not temp_file.exists()  # 取消即清理临时文件
    assert task.temp_paths == ()


def test_task_terminal_state_is_irreversible():
    manager = FileTaskManager()

    async def main():
        task = manager.create("get_file")

        async def factory_impl():
            return 1

        await manager.run(task, lambda: factory_impl())
        with pytest.raises(FileTaskError):
            await manager.run(task, lambda: factory_impl())
        # cancel 对终态忽略（不抛错、不改状态）。
        assert manager.cancel(task.task_id) is False
        assert task.state == FileTaskState.SUCCEEDED
        with pytest.raises(FileTaskError):
            await manager.stream_upload(task, [(0, b"x")], lambda i, d: factory_impl())

    run(main())


def test_task_cancel_pending_task_directly():
    manager = FileTaskManager()
    task = manager.create("upload_file")
    assert manager.cancel(task.task_id) is True
    assert task.state == FileTaskState.CANCELLED
    assert manager.cancel(task.task_id) is False  # 已终态
    assert manager.cancel("no-such-task") is False


def test_task_concurrency_limit_queues():
    manager = FileTaskManager(max_concurrent=1)
    events = []

    async def main():
        async def make(name, delay):
            async def factory_impl():
                events.append(f"start-{name}")
                await asyncio.sleep(delay)
                events.append(f"end-{name}")
                return name

            return factory_impl

        t1 = manager.create("a")
        t2 = manager.create("b")
        f1 = await make("one", 0.08)
        f2 = await make("two", 0.01)
        await asyncio.gather(
            manager.run(t1, lambda: f1()), manager.run(t2, lambda: f2())
        )
        return t1, t2

    t1, t2 = run(main())
    assert t1.state == FileTaskState.SUCCEEDED
    assert t2.state == FileTaskState.SUCCEEDED
    # 上限 1：two 必须等 one 完成才开始（排队而非并发/拒绝）。
    assert events == ["start-one", "end-one", "start-two", "end-two"]


def test_task_queued_cancel_never_runs_factory():
    manager = FileTaskManager(max_concurrent=1)
    calls = {"queued": 0}

    async def main():
        blocker = manager.create("blocker")
        queued = manager.create("queued")

        async def block_impl():
            await asyncio.sleep(0.15)

        async def queued_impl():
            calls["queued"] += 1

        first = asyncio.create_task(manager.run(blocker, lambda: block_impl()))
        second = asyncio.create_task(manager.run(queued, lambda: queued_impl()))
        await asyncio.sleep(0.03)
        assert queued.state == FileTaskState.PENDING  # 仍在排队
        assert manager.cancel(queued.task_id) is True
        with pytest.raises(asyncio.CancelledError):
            await second
        await first
        return queued

    queued = run(main())
    assert queued.state == FileTaskState.CANCELLED
    assert calls["queued"] == 0


def test_temp_path_registration_bounds_and_cleanup(tmp_path):
    manager = FileTaskManager(temp_root=tmp_path)
    task = manager.create("upload_file")
    inside = tmp_path / "part.bin"
    inside.write_bytes(b"1234")
    manager.register_temp_path(task.task_id, inside)
    assert task.temp_paths == (os.path.realpath(str(inside)),)
    with pytest.raises(FileTaskError):
        manager.register_temp_path(task.task_id, "/etc/hostname")  # 越界
    with pytest.raises(FileTaskError):
        manager.register_temp_path("unknown-task", inside)
    removed = manager.cleanup(task)
    assert removed == 1
    assert not inside.exists()
    assert task.temp_paths == ()
    assert manager.cleanup(task) == 0  # 幂等


def test_temp_cleanup_missing_file_not_an_error(tmp_path):
    manager = FileTaskManager(temp_root=tmp_path)
    task = manager.create("upload_file")
    ghost = tmp_path / "ghost.bin"
    ghost.write_bytes(b"x")
    manager.register_temp_path(task.task_id, ghost)
    ghost.unlink()
    assert manager.cleanup(task) == 0
    assert manager.temp_bytes_used == 0


def test_temp_quota_enforced_and_released(tmp_path):
    manager = FileTaskManager(temp_root=tmp_path, max_temp_bytes=10)
    t1 = manager.create("a")
    t2 = manager.create("b")
    f1 = tmp_path / "f1.bin"
    f1.write_bytes(b"123456")  # 6 bytes
    f2 = tmp_path / "f2.bin"
    f2.write_bytes(b"123456")  # 6 bytes, 累计 12 > 10
    manager.register_temp_path(t1.task_id, f1)
    manager.register_temp_path(t1.task_id, f1)  # 重复登记幂等不重复计
    assert manager.temp_bytes_used == 6
    with pytest.raises(FileTaskError):
        manager.register_temp_path(t2.task_id, f2)
    assert manager.temp_bytes_used == 6  # 拒绝不入账
    manager.cleanup(t1)
    assert manager.temp_bytes_used == 0
    manager.register_temp_path(t2.task_id, f2)
    assert manager.temp_bytes_used == 6


# ------------------------------------------------------------ stream upload


def test_stream_upload_progress_with_known_total():
    manager = FileTaskManager()
    sent = []
    progress_at_call = []

    async def main():
        task = manager.create("upload_file")
        chunks = [(0, b"aa"), (1, b"bb"), (2, b"cc"), (3, b"dd")]

        async def send_chunk(index, data):
            progress_at_call.append(task.progress)
            sent.append((index, data))

        result = await manager.stream_upload(task, chunks, send_chunk)
        assert result is task
        return task

    task = run(main())
    assert sent == [(0, b"aa"), (1, b"bb"), (2, b"cc"), (3, b"dd")]
    # 第 k 块调用时 progress 是前 k-1 块的进度（每块成功后才更新）。
    assert progress_at_call == [None, 0.25, 0.5, 0.75]
    assert task.progress == 1.0
    assert task.sent_chunks == 4 and task.sent_bytes == 8
    assert task.total_chunks == 4
    assert task.state == FileTaskState.SUCCEEDED
    assert task.result_code == "ok"


def test_stream_upload_unknown_total_counts_only():
    manager = FileTaskManager()
    progress_at_call = []

    async def main():
        task = manager.create("upload_file")

        async def gen():
            for i in range(3):
                yield (i, b"x" * (i + 1))

        async def send_chunk(index, data):
            progress_at_call.append(task.progress)

        await manager.stream_upload(task, gen(), send_chunk)
        return task

    task = run(main())
    assert progress_at_call == [None, None, None]  # 总数未知 progress 不动
    assert task.sent_chunks == 3 and task.sent_bytes == 6
    assert task.total_chunks is None
    assert task.state == FileTaskState.SUCCEEDED


def test_stream_upload_sync_generator_chunks():
    manager = FileTaskManager()
    sent = []

    async def main():
        task = manager.create("upload_file")

        def gen():
            yield (0, b"a")
            yield (1, b"b")

        async def send_chunk(index, data):
            sent.append(index)

        await manager.stream_upload(task, gen(), send_chunk)
        return task

    task = run(main())
    assert sent == [0, 1]
    assert task.sent_chunks == 2
    assert task.state == FileTaskState.SUCCEEDED


def test_stream_upload_cancel_stops_immediately():
    manager = FileTaskManager()
    calls = []

    async def main():
        task = manager.create("upload_file")
        chunks = [(i, b"x") for i in range(10)]

        async def send_chunk(index, data):
            calls.append(index)
            await asyncio.sleep(0.01)

        runner = asyncio.create_task(manager.stream_upload(task, chunks, send_chunk))
        while len(calls) < 2:
            await asyncio.sleep(0.005)
        assert manager.cancel(task.task_id) is True
        with pytest.raises(asyncio.CancelledError):
            await runner
        count_at_cancel = len(calls)
        await asyncio.sleep(0.05)
        assert len(calls) == count_at_cancel  # 取消后绝不再调 send_chunk
        return task

    task = run(main())
    assert task.state == FileTaskState.CANCELLED
    assert len(calls) < 10


def test_stream_upload_send_error_fails_with_type_name():
    manager = FileTaskManager()
    calls = []

    async def main():
        task = manager.create("upload_file")

        async def send_chunk(index, data):
            calls.append(index)
            if index == 1:
                raise ValueError("nope")

        with pytest.raises(ValueError):
            await manager.stream_upload(task, [(0, b"a"), (1, b"b"), (2, b"c")], send_chunk)
        return task

    task = run(main())
    assert calls == [0, 1]  # 失败即停，不再继续发
    assert task.state == FileTaskState.FAILED
    assert task.result_code == "ValueError"
    assert task.error == "ValueError"
    assert task.sent_chunks == 1


# ------------------------------------------------------------ circuit breaker


def test_circuit_opens_at_threshold_and_blocks():
    breaker = ActionCircuitBreaker(threshold=3, cooldown_seconds=100, clock=FakeClock())
    action = "send_group_file"
    assert breaker.state(action) == "closed"
    assert breaker.allow(action) is True
    breaker.record_failure(action)
    breaker.record_failure(action)
    assert breaker.state(action) == "closed"
    assert breaker.allow(action) is True
    breaker.record_failure(action)
    assert breaker.state(action) == "open"
    assert breaker.allow(action) is False


def test_circuit_success_resets_consecutive_failures():
    breaker = ActionCircuitBreaker(threshold=3, cooldown_seconds=100, clock=FakeClock())
    breaker.record_failure("a")
    breaker.record_failure("a")
    breaker.record_success("a")
    breaker.record_failure("a")
    breaker.record_failure("a")
    assert breaker.state("a") == "closed"
    breaker.record_failure("a")
    assert breaker.state("a") == "open"


def test_circuit_half_open_probe_success_closes():
    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=2, cooldown_seconds=100, clock=clock)
    breaker.record_failure("a")
    breaker.record_failure("a")
    assert breaker.state("a") == "open"
    clock.advance(99)
    assert breaker.state("a") == "open"
    assert breaker.allow("a") is False
    clock.advance(1)
    assert breaker.state("a") == "half_open"
    assert breaker.allow("a") is True  # 放行一次试探
    assert breaker.allow("a") is False  # 试探期间不再放行
    assert breaker.state("a") == "half_open"
    breaker.record_success("a")
    assert breaker.state("a") == "closed"
    assert breaker.allow("a") is True


def test_circuit_half_open_probe_failure_reopens_with_fresh_cooldown():
    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=2, cooldown_seconds=100, clock=clock)
    breaker.record_failure("a")
    breaker.record_failure("a")
    clock.advance(150)
    assert breaker.allow("a") is True
    breaker.record_failure("a")  # 试探失败立即 reopen
    assert breaker.state("a") == "open"
    assert breaker.allow("a") is False
    clock.advance(99)  # 新冷却未满（从 reopen 时刻算）
    assert breaker.state("a") == "open"
    clock.advance(1)
    assert breaker.state("a") == "half_open"
    assert breaker.allow("a") is True


def test_circuit_per_action_isolation_and_reset():
    breaker = ActionCircuitBreaker(threshold=1, cooldown_seconds=100, clock=FakeClock())
    breaker.record_failure("a")
    assert breaker.state("a") == "open"
    assert breaker.state("b") == "closed"
    assert breaker.allow("b") is True
    breaker.reset("a")
    assert breaker.state("a") == "closed"
    assert breaker.allow("a") is True
    # 未知动作默认 closed。
    assert breaker.state("never-seen") == "closed"
    assert breaker.allow("never-seen") is True


def test_circuit_late_failure_while_open_does_not_refresh_cooldown():
    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=1, cooldown_seconds=100, clock=clock)
    breaker.record_failure("a")
    clock.advance(90)
    breaker.record_failure("a")  # open 期迟到失败不得推迟恢复
    clock.advance(10)
    assert breaker.state("a") == "half_open"


def test_real_clock_imports_smoke():
    # 默认时钟参数真实可用（monotonic 口径）。
    breaker = ActionCircuitBreaker()
    assert breaker.allow("x") is True
    resolver = ResourceResolver()
    assert resolver.resolve("id-1").kind == ResourceKind.FILE_ID
    assert time.monotonic() > 0


# ------------------------------------------------- 补审第二段修复


def test_resolver_enforces_max_bytes_for_local_files(tmp_path):
    """P3：本地文件按 stat 真实大小执行 max_bytes——省略或低报
    size_hint 都不豁免；size_hint 缺省时以真实大小落账（修复前
    local_path 分支只查声明大小，100KB 文件可对 1KB 上限放行）。"""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100_000)
    resolver = ResourceResolver(allowed_roots=(tmp_path,), max_bytes=1024)
    with pytest.raises(ResourceValidationError):
        resolver.resolve(str(big))
    with pytest.raises(ResourceValidationError):
        resolver.resolve(str(big), size_hint=1)
    with pytest.raises(ResourceValidationError):
        resolver.resolve(f"file://{big}")
    small = tmp_path / "small.bin"
    small.write_bytes(b"x" * 512)
    resolved = resolver.resolve(str(small))
    assert resolved.kind == ResourceKind.LOCAL_PATH
    assert resolved.size_hint == 512


def test_resolver_local_size_getsize_failure_is_validation_error(
    tmp_path, monkeypatch
):
    """P4：isfile 通过后 getsize 在竞态窗口失败（文件被删/改名/
    改权限）必须收敛为 ResourceValidationError——gateway 与
    ImageFetcher 只捕本异常，裸 OSError 穿透即 resolver 异常
    契约破裂（修复前注入 PermissionError 时 resolve 抛裸
    PermissionError，执行者探针已复现）。"""
    target = tmp_path / "f.bin"
    target.write_bytes(b"x" * 16)
    resolver = ResourceResolver(allowed_roots=(tmp_path,), max_bytes=1024)

    def failing_getsize(_path):
        raise PermissionError("synthetic getsize failure")

    monkeypatch.setattr(os.path, "getsize", failing_getsize)
    with pytest.raises(ResourceValidationError):
        resolver.resolve(str(target))


def test_validate_path_semantic_ref_rules(tmp_path):
    """P2：路径语义分类——相对路径以 base 解析后查 roots、穿越拒、
    无 base 拒；不透明 file_id 与 URL 维持原放行口径。"""
    base = tmp_path / "base"
    base.mkdir()
    inside = base / "docs" / "f.pdf"
    assert validate_path_semantic_ref("plain-file-id-123") == "plain-file-id-123"
    assert (
        validate_path_semantic_ref("https://example.com/f.pdf")
        == "https://example.com/f.pdf"
    )
    # 相对路径在 base 内：放行（文件不必存在，只判 realpath 归属）。
    assert (
        validate_path_semantic_ref("docs/f.pdf", allowed_roots=(base,), base_dir=base)
        == "docs/f.pdf"
    )
    # .. 穿越出 roots：拒绝（正/反斜杠同等对待）。
    with pytest.raises(ResourceValidationError):
        validate_path_semantic_ref(
            "../../data/memory.db", allowed_roots=(base,), base_dir=base
        )
    with pytest.raises(ResourceValidationError):
        validate_path_semantic_ref(
            "..\\..\\secret.txt", allowed_roots=(base,), base_dir=base
        )
    # 无 base 时相对路径无从解析：拒绝（fail closed）。
    with pytest.raises(ResourceValidationError):
        validate_path_semantic_ref("docs/f.pdf", allowed_roots=(base,))
    # 绝对路径界外拒绝、界内放行（与 validate_resource_ref 对称）。
    with pytest.raises(ResourceValidationError):
        validate_path_semantic_ref("/etc/passwd", allowed_roots=(base,), base_dir=base)
    assert (
        validate_path_semantic_ref(str(inside), allowed_roots=(base,), base_dir=base)
        == str(inside)
    )
    # roots 为空：路径形态一律拒绝。
    with pytest.raises(ResourceValidationError):
        validate_path_semantic_ref("docs/f.pdf", base_dir=base)


def test_validate_download_url_and_on_disk_name_rules():
    """P3：download_file 的 SSRF 边界与落盘名 basename 校验。"""
    assert (
        validate_download_url("https://example.com/f.pdf")
        == "https://example.com/f.pdf"
    )
    assert validate_download_url("http://8.8.8.8/f") == "http://8.8.8.8/f"
    # 纯十六进制字符组成的普通 DNS 名不是 IP 字面量（复审 P3
    # 收窄）：标签不全为数字 / 0x 数时放行。
    for good in (
        "https://abc123.de/f",
        "https://cafe123.de/f",
        "https://abc123.cafe/f",
        "https://deadbeef1.cafe/f",
        # NAT64 内嵌公网 IPv4：解包复核后为公网，放行（复审
        # 第 3 轮 P3，与 vision 共享 _is_public_ip 的口径）。
        "http://[64:ff9b::808:808]/f",
        # 全角字母域名：WHATWG 归一后即普通公网域名，放行。
        "https://\uff41bc123.de/f",
        # 正常 IDN 域名（原文与 punycode 形态）：NFKC 折叠与
        # 忽略字符拒绝不得误杀（复审第 4 轮 P2 正例钉子）。
        "https://\u4f8b\u5b50.\u4e2d\u56fd/f",
        "https://xn--fiqs8s.cn/f",
        # NFKC 折叠型公网正例（复审第 4 轮 P4 正例钉子）：字符
        # 本身会被 NFKC 折叠（圆圈数字/上标/fi 连字/半角片假
        # 名），但折叠后是普通公网域名——不得被兼容折叠与忽略
        # 字符拒绝误杀。
        "https://\u2460.example.com/f",
        "https://x\u00b2.example.com/f",
        "https://\ufb01le.example.com/f",
        "https://\uff76\uff80\uff76\uff85.example.com/f",
    ):
        assert validate_download_url(good) == good
    for bad in (
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:3000/get_status",
        "http://192.168.1.1/admin",
        "http://10.0.0.5/f",
        "http://[::1]/f",
        "http://localhost/f",
        "http://printer.home.arpa/f",
        "http://nas.local/f",
        "http://2130706433/f",
        "http://0x7f.1/f",
        "http://0177.0.0.1/f",
        "http://127.1/f",
        # 等价形态绕过（复审 P2）：尾点、*.localhost、百分号编码
        # 与被剥尾点后的内网后缀，都与规范形态同罪。
        "http://localhost./f",
        "http://localhost../f",
        "http://sub.localhost/f",
        "http://sub.localhost./f",
        "http://evil.local./f",
        "http://foo.internal./f",
        "http://x.home.arpa./f",
        "http://%31%32%37.0.0.1/f",
        "ftp://example.com/f",
        # WHATWG 等价形态（复审第 3 轮 P2）：点等价字符（U+3002）
        # 与全角数字/字母在平台抓取方归一后即规范内网形态——校
        # 验前先同口径归一，归一后与规范形态同罪。
        "http://127\u30020\u30020\u30021/f",
        "http://\uff11\uff12\uff17.0.0.1/f",
        "http://localhost\u3002/f",
        "http://169.254.169.254\u3002/f",
        "http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54/f",
        # NAT64 内嵌私有目标与显式拒绝网段（复审第 3 轮 P3）：
        # stdlib is_global 对这些形态为真，须过共享 _is_public_ip
        # 的解包复核与拒绝网段表。
        "http://[64:ff9b::a9fe:a9fe]/f",
        "http://[64:ff9b::7f00:1]/f",
        "http://224.0.0.1/f",
        "http://192.88.99.1/f",
        # 拒绝网段的 WHATWG 编码形态（复审第 4 轮 P4 钉子）：
        # 224.0.0.1 与 192.88.99.1 的 stdlib is_global 为真，十
        # 进制/十六进制整数字面量只能靠 WHATWG 分支上的共享
        # _is_public_ip 拒绝——该分支若被降级为 is_global，本
        # 组用例必须转红。
        "http://3758096385/f",
        "http://0xe0000001/f",
        "http://3227017985/f",
        # 点等价字符 U+FF0E / U+FF61（复审第 4 轮 P4 附带钉子）：
        # 与 U+3002 同为标签分隔点等价形，归一后同罪。
        "http://127\uff0e0\uff0e0\uff0e1/f",
        "http://127\uff610\uff610\uff611/f",
        # UTS-46 默认忽略字符（复审第 4 轮 P2）：抓取方删掉后
        # 再连接（127/localhost/*.internal），校验侧 fail-closed
        # 直接拒绝；兼容映射数字形（圆圈数字/上标）经 NFKC 折
        # 叠后即规范回环形态、同罪。
        "http://127\u200b.0.0.1/f",
        "http://local\u200bhost/f",
        "http://127\ufeff.0.0.1/f",
        "http://127\u00ad.0.0.1/f",
        "http://a.interna\u200bl/f",
        "http://\u2460\u2461\u2466.0.0.1/f",
        "http://127.0.0.\u00b9/f",
        # 默认忽略字符漏段（复审第 4 轮 P2）：速记格式控制
        # （U+1BCA0–1BCA3）与乐谱格式控制（U+1D173 起三段）
        # 同为 Default_Ignorable_Code_Point 且被 UTS46 删除，
        # 第 4 轮手列名单漏收、第 5 轮补入——本组形态在补入
        # 前全部 ALLOW（法官与执行者均已复现）。
        "http://127\U0001bca0.0.0.1/f",
        "http://local\U0001bca0host/f",
        "http://127\U0001d173.0.0.1/f",
        "http://local\U0001d173host/f",
        "http://a.interna\U0001d173l/f",
        "http://127\U0001d185.0.0.1/f",
        "http://127\U0001d1aa.0.0.1/f",
        # 归一后第二道检查（复审第 4 轮 P4）：U+3164 本身不在
        # 忽略表内，NFKC 折叠为 U+1160（表内）才命中——只有
        # 归一后复查能拦下本形态，删第二道检查即转红。
        "http://127\u3164.0.0.1/f",
        # 空标签畸形（复审 P4）：双点主机名不许以「标签不全数
        # 字」为由漏过非规范字面量判定。
        "http://127..0.1/f",
        # 畸形 URL（复审 P3）：urlparse 的裸 ValueError 必须转
        # ResourceValidationError——pytest.raises 按子类型断言，
        # 裸 ValueError 穿透即本用例转红。
        "http://[::1/x",
    ):
        with pytest.raises(ResourceValidationError):
            validate_download_url(bad)
    assert validate_on_disk_name("f.pdf") == "f.pdf"
    for bad in ("../../tmp/x", "a/b", "a\\b", "..", ".", ""):
        with pytest.raises(ResourceValidationError):
            validate_on_disk_name(bad)


def test_download_url_ignorable_ranges_each_pinned():
    """默认忽略字符逐段钉子（复审第 4 轮 P4）：第 4 轮只抽样钉了
    15 段中的 3 段代表元，未抽中的段被删/写错时全套绿灯——正是
    漏段 P2 的成因。本用例按 _DEFAULT_IGNORABLE_RANGES 逐段取
    首尾代表元，嵌进回环形态逐个断言拒绝；今后表里增删段，本
    用例自动跟随（完备性本身由下方删除集扫描把关，两者分工：
    本用例钉「表里每段都真生效」，扫描钉「表外无漏网删除
    集」）。"""
    for low, high in _DEFAULT_IGNORABLE_RANGES:
        for codepoint in {low, high}:
            host = f"127{chr(codepoint)}.0.0.1"
            with pytest.raises(ResourceValidationError):
                validate_download_url(f"http://{host}/f")
    # 段外正例对照：紧邻段界外侧的码点不在忽略表内、嵌进普通
    # 公网域名不得被误杀（防名单过宽向外蔓延）。
    for codepoint in (0x1BCA4, 0xE01F0):
        good = f"https://ex{chr(codepoint)}ample.com/f"
        assert validate_download_url(good) == good
    # 归一后第二道检查的直达证明（复审第 4 轮 P4）：U+3164 归
    # 一前不在表内、NFKC 折叠为 U+1160（表内）后才命中——本形
    # 态只有 validate_download_url 的第二道 _contains_default_
    # ignorable 检查能拦下，删掉它即转红（上方 bad 列表同形态
    # 的行为断言与此处的机理断言互为表里）。
    assert not _contains_default_ignorable("\u3164")
    assert _contains_default_ignorable(
        _normalize_whatwg_host("127\u3164.0.0.1")
    )


def _uts46_deleted_codepoints() -> list[int]:
    """UTS46 删除集全集语料：仓库自带快照
    （tests/uts46_ignored_snapshot.py）——生成自 idna 包
    IdnaMappingTable 数据中状态为 ignored（抓取方处理时直接
    删除）的全部码点，由下方对账用例与 idna 现表逐点核对。
    扫描不直接读第三方包内部数据表：完备性根基由仓库自身
    持有（复审第 5 轮 P4，原实现直读未声明的传递依赖 idna）。"""
    return list(UTS46_IGNORED_CODEPOINTS)


def test_download_url_rejects_full_uts46_deletion_set():
    """完备性回归扫描（复审第 4 轮 P2 的根治件）：手列名单曾漏
    速记/乐谱格式控制段（U+1BCA0 等 12 个码点），靠逐段抽样钉
    子发现不了。本扫描以 UTS46 删除集全集为语料，把每个删除集
    字符分别嵌进三类底（127 分段 / localhost / *.internal），
    断言 validate_download_url 一律拒绝——校验名单今后漏任何
    一段、或归一管线对某字符失效，本扫描立即变红。语料为仓
    库自带快照（与 idna 包 IdnaMappingTable 的 ignored 全集
    逐点对账，见下方对账用例）；另以 Node WHATWG URL 对照抽
    验过 1BCA0/1D173 等代表元的删除行为一致（第 5 轮执行者
    探针）。"""
    corpus = _uts46_deleted_codepoints()
    # 语料健全性下限与本轮 P2 码点在册断言：语料推导若损坏
    # （如状态字节口径变更）导致语料缩水/漏掉本轮码点，扫描
    # 会在空集上假绿——先钉死语料本身可信。
    assert len(corpus) >= 250
    assert 0x1BCA0 in corpus and 0x1D173 in corpus
    failures = []
    for codepoint in corpus:
        char = chr(codepoint)
        for url in (
            f"http://127{char}.0.0.1/f",
            f"http://local{char}host/f",
            f"http://a.interna{char}l/f",
        ):
            try:
                validate_download_url(url)
            except ResourceValidationError:
                continue
            failures.append((hex(codepoint), url))
    assert failures == []


def test_uts46_snapshot_matches_idna_table():
    """快照对账（复审第 5 轮 P4）：仓库快照必须与 idna 包
    IdnaMappingTable 现表的 ignored 全集逐点一致——上游数据
    口径漂移时在此响亮失败、提醒刷新快照，而不是让完备性
    扫描在不知情中失守。idna 不可用时本对账 skip：快照与扫
    描不依赖 idna，照常工作（本用例只守对账、不守扫描）。"""
    uts46data = pytest.importorskip("idna.uts46data")
    derived: set[int] = set()
    starts = uts46data.uts46_starts
    statuses = uts46data.uts46_statuses
    for index, start in enumerate(starts):
        if statuses[index] == ord("I"):
            end = starts[index + 1] if index + 1 < len(starts) else 0x110000
            derived.update(range(start, end))
    assert derived == set(UTS46_IGNORED_CODEPOINTS)


def test_stream_upload_respects_max_concurrent():
    """P3：stream_upload 与 run 共用并发信号量——max_concurrent=1
    时两个并发流式上传的 send_chunk 峰值并发为 1（修复前为 2，
    上限形同虚设）。"""
    manager = FileTaskManager(max_concurrent=1)
    state = {"current": 0, "peak": 0}

    async def send_chunk(_index, _data):
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.02)
        state["current"] -= 1

    async def main():
        t1 = manager.create("upload_file_stream")
        t2 = manager.create("upload_file_stream")
        await asyncio.gather(
            manager.stream_upload(t1, [(0, b"a"), (1, b"b")], send_chunk),
            manager.stream_upload(t2, [(0, b"c"), (1, b"d")], send_chunk),
        )
        return t1, t2

    t1, t2 = run(main())
    assert state["peak"] == 1
    assert t1.state == FileTaskState.SUCCEEDED
    assert t2.state == FileTaskState.SUCCEEDED


def test_stream_upload_timeout_marks_timed_out():
    """P3 附带口径对齐：stream_upload 的 timeout 与 run 同语义
    （到期落 timed_out 并上抛 TimeoutError）。"""
    manager = FileTaskManager()

    async def slow_send(_index, _data):
        await asyncio.sleep(5)

    async def main():
        task = manager.create("upload_file_stream")
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await manager.stream_upload(task, [(0, b"a")], slow_send, timeout=0.05)
        return task

    task = run(main())
    assert task.state == FileTaskState.TIMED_OUT
    assert task.result_code == "timeout"


def test_stream_upload_callback_timeout_error_is_failure_without_deadline():
    """复审 P4：无 timeout 配置时 send_chunk 自身抛出的
    TimeoutError 不是 deadline 到期——落 FAILED/result_code=
    TimeoutError，不得冒充 TIMED_OUT（run() 同口径一并钉住）。"""
    manager = FileTaskManager()

    async def boom(_index, _data):
        raise TimeoutError("chunk socket timeout")

    async def main():
        task = manager.create("upload_file_stream")
        with pytest.raises(TimeoutError):
            await manager.stream_upload(task, [(0, b"a")], boom)
        return task

    task = run(main())
    assert task.state == FileTaskState.FAILED
    assert task.result_code == "TimeoutError"

    async def failing_factory():
        raise TimeoutError("factory socket timeout")

    async def main2():
        task = manager.create("upload_file")
        with pytest.raises(TimeoutError):
            await manager.run(task, failing_factory)
        return task

    task = run(main2())
    assert task.state == FileTaskState.FAILED
    assert task.result_code == "TimeoutError"


def _queued_stream_scenario(cancel_mode):
    """max_concurrent=1：第一个流式上传卡在 send_chunk，第二个
    排队等待信号量；按 cancel_mode 取消第二个并断言其终态。"""
    manager = FileTaskManager(max_concurrent=1)
    started = asyncio.Event()
    release = asyncio.Event()
    sent = []

    async def blocking_send(index, data):
        sent.append(("first", index))
        started.set()
        await release.wait()

    async def second_send(index, data):
        sent.append(("second", index))

    async def main():
        t1 = manager.create("upload_file_stream")
        t2 = manager.create("upload_file_stream")
        task1 = asyncio.create_task(
            manager.stream_upload(t1, [(0, b"a")], blocking_send)
        )
        await started.wait()
        task2 = asyncio.create_task(
            manager.stream_upload(t2, [(0, b"b")], second_send)
        )
        # 让 task2 跑到信号量排队点。
        await asyncio.sleep(0.1)
        if cancel_mode == "manager":
            assert manager.cancel(t2.task_id) is True
        elif cancel_mode == "task":
            task2.cancel()
        elif cancel_mode == "terminate":
            # 协程未被取消、记录先被外部终结（白盒模拟关停清扫等
            # 其他终结者）：获信号量后终态复检必须拦下发送。
            manager._set_terminal(t2, FileTaskState.CANCELLED, result_code="cancelled")
        release.set()
        results = await asyncio.gather(task1, task2, return_exceptions=True)
        return t1, t2, results

    return manager, sent, main


def test_stream_upload_queued_cancel_via_manager():
    """复审 P4：排队中的 stream_upload 经 manager.cancel() 取消
    ——落 CANCELLED/'cancelled'、零块发送（排队取消处理分支）。"""
    _manager, sent, main = _queued_stream_scenario("manager")
    t1, t2, results = run(main())
    assert t1.state == FileTaskState.SUCCEEDED
    assert t2.state == FileTaskState.CANCELLED
    assert t2.result_code == "cancelled"
    assert isinstance(results[1], asyncio.CancelledError)
    assert [s for s in sent if s[0] == "second"] == []


def test_stream_upload_queued_cancel_via_task():
    """复审 P4：直接取消排队协程的 asyncio.Task 同口径落
    CANCELLED/'cancelled'、零块发送。"""
    _manager, sent, main = _queued_stream_scenario("task")
    t1, t2, results = run(main())
    assert t1.state == FileTaskState.SUCCEEDED
    assert t2.state == FileTaskState.CANCELLED
    assert t2.result_code == "cancelled"
    assert isinstance(results[1], asyncio.CancelledError)
    assert [s for s in sent if s[0] == "second"] == []


def test_stream_upload_terminal_recheck_blocks_send():
    """复审 P4：排队期间记录被外部终结（协程未取消）时，获信号量
    后的终态复检必须拦下发送并以 FileTaskError 收口，记录终态
    不被覆盖。"""
    _manager, sent, main = _queued_stream_scenario("terminate")
    t1, t2, results = run(main())
    assert t1.state == FileTaskState.SUCCEEDED
    assert t2.state == FileTaskState.CANCELLED
    assert t2.result_code == "cancelled"
    assert isinstance(results[1], FileTaskError)
    assert [s for s in sent if s[0] == "second"] == []
