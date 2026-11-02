"""N5 Claude Code Review 修复回归（逐条对应 findings）。

- P1-1：half-open 试探被取消/超时后名额必须结清，动作不得熔断死锁；
- P2-1：派发点 circuit_open 不得烧确认令牌、账本记 aborted 可重试，
  且 open 状态在令牌消费前复检拒绝；
- P2-2：文件域新资源键（file_path/files 等）纳入网关资源边界；
- P2-3：resolver 接线 fail closed（dict 资源、路径语义键的
  file_id 分类）；
- P3-2：临时文件跨任务引用计数（配额不残留、文件不被误删）；
- P3-3：stream_upload 对 RUNNING 任务重入拒绝；
- P3-4：熔断迟到结果不影响在飞试探（世代号记账）；
- P3-5：畸形 Unicode 资源值不让 invoke 在门禁前抛错；
- P3-6：ResolvedResource/FileTask 的 repr 不泄漏原文/真实路径；
- P3-7：门面本地拒绝（do_group_album_comment 双缺）补审计行；
- P3-8：Raw node/forward 嵌套夹带 receive-only 段出站拒绝。
"""

import asyncio
import logging

import pytest

from companion.adapters import segments_to_onebot_message
from companion.adapters.base import RawSegment
from companion.adapters.circuit import ActionCircuitBreaker
from companion.adapters.fake import FakeAdapter, FakeTransport
from companion.adapters.file_tasks import (
    FileTaskError,
    FileTaskManager,
    FileTaskState,
)
from companion.adapters.gateway import (
    ActionGateway,
    AuthorizationContext,
    params_digest,
)
from companion.adapters.resources import ResourceResolver

pytestmark = pytest.mark.timeout(10)

GROUP = "80001"
USER = "90001"
ALBUM = "alb-1"
ALBUM_ACTION = "upload_image_to_qun_album"
ALBUM_PARAMS = {
    "group_id": GROUP,
    "album_id": ALBUM,
    "album_name": "相册",
    "file": "https://example.com/a.png",
}
OK = {"status": "ok", "retcode": 0, "data": {}}
FAILED = {"status": "failed", "retcode": 1200, "data": None}


def run(coro):
    return asyncio.run(coro)


class FakeClock:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class ModeTransport:
    """可切换模式的传输替身：fail → 平台拒绝；hang → 永不返回；
    ok → 成功。记录全部调用。"""

    def __init__(self):
        self.calls = []
        self.mode = "fail"

    async def _respond(self, action, params):
        self.calls.append(action)
        if self.mode == "hang":
            await asyncio.Event().wait()
        if self.mode == "fail":
            return FAILED
        return OK

    async def post_action(self, action, params=None):
        return await self._respond(action, params)

    async def get_action(self, action, params=None):
        return await self._respond(action, params)


def _album_gateway(transport, clock, **kw):
    breaker = ActionCircuitBreaker(threshold=3, cooldown_seconds=300, clock=clock)
    gateway = ActionGateway(
        transport,
        policies={ALBUM_ACTION: {"enabled": True}},
        circuit_breaker=breaker,
        **kw,
    )
    return gateway, breaker


# ---------------------------------------------------------------- P1-1


def test_probe_cancelled_does_not_deadlock_circuit():
    async def main():
        clock = FakeClock()
        transport = ModeTransport()
        gateway, breaker = _album_gateway(transport, clock)

        async def invoke():
            return await gateway.invoke(
                ALBUM_ACTION, ALBUM_PARAMS, principal="system", rate_limit=1000
            )

        for _ in range(3):
            assert (await invoke()).outcome == "rejected"
        assert breaker.state(ALBUM_ACTION) == "open"
        clock.advance(301)
        transport.mode = "hang"
        probe = asyncio.create_task(invoke())
        await asyncio.sleep(0.1)
        assert breaker.probe_generation(ALBUM_ACTION) is not None
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe
        # 试探被 abandon 结清：回到 open（刷新冷却），不卡 half_open。
        assert breaker.state(ALBUM_ACTION) == "open"
        assert breaker.probe_generation(ALBUM_ACTION) is None
        calls_before = len(transport.calls)
        assert (await invoke()).gate == "circuit_open"
        assert len(transport.calls) == calls_before  # 零派发
        clock.advance(301)
        transport.mode = "ok"
        assert (await invoke()).outcome == "ok"  # 可再次试探并恢复
        assert breaker.state(ALBUM_ACTION) == "closed"

    run(main())


# ---------------------------------------------------------------- P2-1


def test_open_circuit_rejects_before_token_consume_and_ledger():
    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=3, cooldown_seconds=300, clock=clock)
    for _ in range(3):
        breaker.record_failure("del_group_album_media")
    assert breaker.state("del_group_album_media") == "open"
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={"del_group_album_media": {"enabled": True, "dry_run": False}},
        circuit_breaker=breaker,
    )
    params = {"group_id": GROUP, "album_id": ALBUM, "lloc": "lloc-1"}
    token = gateway.issue_confirmation("del_group_album_media", params)
    auth = AuthorizationContext(authorizer="owner-1", groups=frozenset({GROUP}))
    result = run(
        gateway.invoke(
            "del_group_album_media",
            params,
            principal="system",
            authorization=auth,
            confirmation_token=token,
            operation_id="op-p21-open",
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "circuit_open")
    assert transport.calls == []
    # 账本未占位、令牌未消费。
    assert gateway.operation_ledger.lookup("op-p21-open") is None
    assert gateway.confirmation_tokens.peek(
        token,
        action="del_group_album_media",
        target=f"group:{GROUP}",
        params_digest=params_digest(params),
    )


def test_dispatch_time_circuit_open_aborts_ledger_and_retries():
    async def main():
        clock = FakeClock()
        transport = ModeTransport()
        gateway, breaker = _album_gateway(transport, clock)

        async def invoke(**kw):
            return await gateway.invoke(
                ALBUM_ACTION, ALBUM_PARAMS, principal="system",
                rate_limit=1000, **kw,
            )

        for _ in range(3):
            await invoke()
        assert breaker.state(ALBUM_ACTION) == "open"
        clock.advance(301)
        transport.mode = "hang"
        probe = asyncio.create_task(invoke())
        await asyncio.sleep(0.1)
        # 试探在飞：第二个请求走到派发点被 allow() 拒。
        raced = await invoke(operation_id="op-p21-race")
        assert (raced.outcome, raced.gate) == ("rejected", "circuit_open")
        record = gateway.operation_ledger.lookup("op-p21-race")
        assert record is not None and record.state == "aborted"
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe
        clock.advance(301)
        transport.mode = "ok"
        retried = await invoke(operation_id="op-p21-race")
        assert retried.outcome == "ok"  # aborted 可同 id 重试，非 replay_blocked

    run(main())


# ---------------------------------------------------------------- P2-2


def test_new_resource_keys_enforced_by_gateway_roots(tmp_path):
    allowed_file = tmp_path / "ok.png"
    allowed_file.write_bytes(b"x")
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={
            "send_online_file": {"enabled": True},
            "upload_images_to_qun_album": {"enabled": True},
        },
        resource_roots=(str(tmp_path),),
    )
    result = run(
        gateway.invoke(
            "send_online_file",
            {"user_id": USER, "file_path": "/etc/passwd"},
            principal="system",
            rate_limit=1000,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    result = run(
        gateway.invoke(
            "upload_images_to_qun_album",
            {
                "group_id": GROUP, "album_id": ALBUM, "album_name": "相册",
                "files": ["/etc/passwd"],
            },
            principal="system",
            rate_limit=1000,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert transport.calls == []
    result = run(
        gateway.invoke(
            "upload_images_to_qun_album",
            {
                "group_id": GROUP, "album_id": ALBUM, "album_name": "相册",
                "files": [str(allowed_file)],
            },
            principal="system",
            rate_limit=1000,
        )
    )
    assert result.outcome == "ok"


# ---------------------------------------------------------------- P2-3


def test_resolver_hook_fails_closed_on_dict_and_path_semantic_file_id(tmp_path):
    transport = FakeTransport()
    gateway = ActionGateway(
        transport,
        policies={
            "send_online_file": {"enabled": True},
            "upload_group_file": {"enabled": True},
        },
        resource_resolver=ResourceResolver(allowed_roots=(str(tmp_path),)),
    )
    # 路径语义键的相对串被 resolver 分类为 file_id → 拒绝。
    result = run(
        gateway.invoke(
            "send_online_file",
            {"user_id": USER, "file_path": "relative/secret.txt"},
            principal="system",
            rate_limit=1000,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    # 资源键下 dict 形态 → fail closed（不跳过）。
    result = run(
        gateway.invoke(
            "upload_group_file",
            {"group_id": GROUP, "file": {"name": "x"}, "name": "x"},
            principal="system",
            rate_limit=1000,
        )
    )
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert transport.calls == []


# ------------------------------------------------------- P3-2 / P3-3


def test_temp_path_refcount_across_tasks(tmp_path):
    manager = FileTaskManager(temp_root=tmp_path)
    shared = tmp_path / "shared.bin"
    shared.write_bytes(b"0123456789")
    first = manager.create("upload_group_file")
    second = manager.create("download_file")
    manager.register_temp_path(first.task_id, shared)
    manager.register_temp_path(second.task_id, shared)
    assert manager.temp_bytes_used == 10  # 只记一份
    manager.cleanup(first)
    assert shared.exists()  # 仍被 second 引用，不误删
    assert manager.temp_bytes_used == 10
    manager.cleanup(second)
    assert not shared.exists()
    assert manager.temp_bytes_used == 0  # 配额不残留


def test_stream_upload_rejects_running_task():
    manager = FileTaskManager()
    task = manager.create("upload_file_stream")
    task.state = FileTaskState.RUNNING  # 模拟已有执行者在跑

    async def send_chunk(index, data):
        raise AssertionError("must not be called")

    with pytest.raises(FileTaskError):
        run(manager.stream_upload(task, [(0, b"x")], send_chunk))


# ---------------------------------------------------------------- P3-4


def test_circuit_late_results_do_not_touch_inflight_probe():
    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=3, cooldown_seconds=300, clock=clock)
    for _ in range(3):
        breaker.record_failure("act")
    assert breaker.state("act") == "open"
    clock.advance(301)
    assert breaker.allow("act") is True
    gen = breaker.probe_generation("act")
    assert gen is not None
    # 迟到成功（世代不匹配）不得提前关闭电路。
    breaker.record_success("act", probe_gen=gen + 99)
    assert breaker.state("act") == "half_open"
    assert breaker.probe_generation("act") == gen
    # 迟到失败（-1 永不匹配）不得刷新冷却/清试探。
    opened_at_before = clock.t
    breaker.record_failure("act", probe_gen=-1)
    assert breaker.probe_generation("act") == gen
    clock.advance(10)
    assert breaker.state("act") == "half_open"
    assert opened_at_before == 1301.0
    # 本试探的成功正常关闭。
    breaker.record_success("act", probe_gen=gen)
    assert breaker.state("act") == "closed"


def test_circuit_abandon_probe_reopens_with_fresh_cooldown():
    clock = FakeClock()
    breaker = ActionCircuitBreaker(threshold=3, cooldown_seconds=300, clock=clock)
    for _ in range(3):
        breaker.record_failure("act")
    clock.advance(301)
    assert breaker.allow("act") is True
    gen = breaker.probe_generation("act")
    breaker.abandon_probe("act", probe_gen=gen + 5)  # 世代不符不动
    assert breaker.probe_generation("act") == gen
    breaker.abandon_probe("act", probe_gen=gen)
    assert breaker.state("act") == "open"
    assert breaker.allow("act") is False  # 冷却已刷新
    clock.advance(301)
    assert breaker.allow("act") is True


# ---------------------------------------------------------------- P3-5


def test_malformed_unicode_resource_does_not_raise():
    transport = FakeTransport()
    gateway = ActionGateway(
        transport, policies={"upload_group_file": {"enabled": True}}
    )
    result = run(
        gateway.invoke(
            "upload_group_file",
            {"group_id": GROUP, "file": "\ud800abc", "name": "x"},
            principal="system",
            rate_limit=1000,
        )
    )
    assert result.outcome in {"ok", "rejected", "ambiguous", "simulated"}


# ---------------------------------------------------------------- P3-6


def test_reprs_do_not_leak_raw_refs(tmp_path):
    resolver = ResourceResolver(allowed_roots=(str(tmp_path),))
    secret_url = "https://example.com/f?sign=SECRETVALUE"
    resolved = resolver.resolve(secret_url)
    assert "SECRETVALUE" not in repr(resolved)
    assert resolved.digest in repr(resolved)
    manager = FileTaskManager(temp_root=tmp_path)
    task = manager.create("download_file")
    path = tmp_path / "secret-name.bin"
    path.write_bytes(b"x")
    manager.register_temp_path(task.task_id, path)
    assert "secret-name.bin" not in repr(task)


# ---------------------------------------------------------------- P3-7


def test_facade_local_rejection_writes_audit_line(caplog):
    fake = FakeAdapter()
    with caplog.at_level(logging.INFO, logger="companion.actions"):
        result = run(
            fake.actions.album.do_group_album_comment(GROUP, ALBUM, "你好")
        )
    assert (result.outcome, result.gate) == ("rejected", "invalid_params")
    assert "do_group_album_comment" in caplog.text
    assert "invalid_params" in caplog.text


# ---------------------------------------------------------------- P3-8


def test_raw_node_forward_nested_receive_only_rejected():
    node = RawSegment(
        "node",
        {"content": [{"type": "onlinefile", "data": {"msgId": "1"}}]},
    )
    assert segments_to_onebot_message([node]) is None
    forward = RawSegment(
        "forward",
        {"content": [{"type": "flashtransfer", "data": {"fileSetId": "x"}}]},
    )
    assert segments_to_onebot_message([forward]) is None
