"""N1 Codex 合并 Review 修复回归（主管裁决 1–4、6–8；5 号 Reject 不动）。

- 修复 1：policy_key 别名白名单（policy_key_forbidden）+ 额度双重派生。
- 修复 2+3：账本 try_begin 原子化、aborted 可重试、默认账本、
  Sqlite 账本跨实例持久。
- 修复 4：资源校验递归化（CQ 串 / segments 嵌套 / 普通文本不误伤）。
- 修复 6：shim 空响应 {} 原样返回。
- 修复 7：门面 send_private identity 派生；send_group 硬关（N1 时
  为门面一律拒；N2 改判为 Gateway policy 默认关，见该用例内注记）。
- 修复 8：审计字段 _audit_token 清洗。
全部对 stub transport 运行：不出网、不发真实消息。
"""

import asyncio
import logging

import pytest

from companion.adapters import FakeAdapter
from companion.adapters.gateway import (
    ActionGateway,
    InMemoryOperationLedger,
    SqliteOperationLedger,
)
from companion.quota import SendQuota
from companion.tools import HttpOneBotClient

pytestmark = pytest.mark.timeout(10)

ENABLED_PRIVATE = {"send_private_msg": {"enabled": True}}


class StubTransport:
    def __init__(self, response=None, delay: float = 0.0):
        self.response = (
            {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
            if response is None
            else response
        )
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []

    async def post_action(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response

    async def get_action(self, action):
        self.calls.append((action, {}))
        return self.response


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ 修复 1


def test_forged_policy_key_cannot_bypass_disabled_action():
    async def scenario():
        stub = StubTransport()
        gateway = ActionGateway(
            stub,
            policies={"send_private_msg": {"enabled": False}},
            quota=SendQuota(),
        )
        # send_private_msg 已停用：借 get_status 的 policy_key 调用，
        # 白名单先行拒绝，绝不派发、绝不走 get_status 的开关。
        forged = await gateway.invoke(
            "send_private_msg",
            {"user_id": "1", "message": "hi"},
            principal="system",
            policy_key="get_status",
        )
        assert (forged.outcome, forged.gate) == ("rejected", "policy_key_forbidden")
        assert stub.calls == []

    run(scenario())


def test_alert_alias_still_walks_quota_path():
    async def scenario():
        stub = StubTransport()
        quota = SendQuota()
        gateway = ActionGateway(
            stub,
            policies={
                "send_private_msg": {"enabled": True},
                "alert_private_msg": {"enabled": True},
            },
            quota=quota,
        )
        params = {"user_id": "10001", "message": "hi"}
        # 合法别名 + 外来 token：额度段拒绝，证明别名仍走额度路径。
        foreign = SendQuota().reserve("10001", 1).consume()
        rejected = await gateway.invoke(
            "send_private_msg",
            params,
            principal="system",
            policy_key="alert_private_msg",
            identity="10001",
            priority="critical",
            quota_token=foreign,
        )
        assert (rejected.outcome, rejected.gate) == ("rejected", "quota_unavailable")
        assert stub.calls == []
        # 合法别名正常派发。
        sent = await gateway.invoke(
            "send_private_msg",
            params,
            principal="system",
            policy_key="alert_private_msg",
            identity="10001",
            priority="critical",
        )
        assert sent.outcome == "ok" and len(stub.calls) == 1

    run(scenario())


# ------------------------------------------------------------ 修复 2+3


def test_concurrent_same_operation_id_dispatches_once():
    async def scenario():
        stub = StubTransport(delay=0.1)
        gateway = ActionGateway(stub, policies=ENABLED_PRIVATE)
        params = {"user_id": "1", "message": "hi"}
        first, second = await asyncio.gather(
            gateway.invoke(
                "send_private_msg", params, principal="system", operation_id="op-conc"
            ),
            gateway.invoke(
                "send_private_msg", params, principal="system", operation_id="op-conc"
            ),
        )
        assert len(stub.calls) == 1
        outcomes = sorted((r.outcome, r.gate) for r in (first, second))
        # 一路派发成功；另一路在途被 replay_blocked（或读到 done 记录）。
        assert ("ok", None) in outcomes
        other = [o for o in outcomes if o != ("ok", None)]
        assert other == [("rejected", "replay_blocked")] or other == [("ok", None)]

    run(scenario())


def test_quota_gate_failure_aborts_and_same_id_can_retry():
    async def scenario():
        stub = StubTransport()
        quota = SendQuota()
        gateway = ActionGateway(stub, policies=ENABLED_PRIVATE, quota=quota)
        params = {"user_id": "10001", "message": "hi"}
        foreign = SendQuota().reserve("10001", 1).consume()
        failed = await gateway.invoke(
            "send_private_msg",
            params,
            principal="system",
            identity="10001",
            quota_token=foreign,
            operation_id="op-retry",
        )
        assert (failed.outcome, failed.gate) == ("rejected", "quota_unavailable")
        assert stub.calls == []
        record = gateway.operation_ledger.lookup("op-retry")
        assert record is not None and record.state == "aborted"
        # aborted 语义：同 id 重试可成功派发。
        retried = await gateway.invoke(
            "send_private_msg",
            params,
            principal="system",
            identity="10001",
            operation_id="op-retry",
        )
        assert retried.outcome == "ok" and len(stub.calls) == 1
        record = gateway.operation_ledger.lookup("op-retry")
        assert record is not None and record.state == "done"

    run(scenario())


def test_default_ledger_exists_without_injection():
    async def scenario():
        stub = StubTransport()
        gateway = ActionGateway(stub, policies=ENABLED_PRIVATE)
        assert isinstance(gateway.operation_ledger, InMemoryOperationLedger)
        params = {"user_id": "1", "message": "hi"}
        first = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-default"
        )
        replay = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-default"
        )
        assert first.outcome == "ok" and replay.outcome == "ok"
        assert len(stub.calls) == 1

    run(scenario())


def test_sqlite_ledger_survives_reopen(tmp_path):
    async def scenario():
        path = tmp_path / "nested" / "ops.db"  # 父目录不存在：构造内部兜底建目录
        stub = StubTransport()
        ledger = SqliteOperationLedger(path)
        gateway = ActionGateway(stub, policies=ENABLED_PRIVATE, operation_ledger=ledger)
        params = {"user_id": "1", "message": "hi"}
        first = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-persist"
        )
        assert first.outcome == "ok" and len(stub.calls) == 1
        ledger.close()

        reopened = SqliteOperationLedger(path)
        record = reopened.lookup("op-persist")
        assert record is not None and record.state == "done" and record.outcome == "ok"
        gateway2 = ActionGateway(
            stub, policies=ENABLED_PRIVATE, operation_ledger=reopened
        )
        replay = await gateway2.invoke(
            "send_private_msg", params, principal="system", operation_id="op-persist"
        )
        assert replay.outcome == "ok" and len(stub.calls) == 1
        reopened.close()

    run(scenario())


# ------------------------------------------------------------ 修复 4


def test_nested_resource_refs_rejected_and_plain_text_untouched():
    async def scenario():
        stub = StubTransport()
        gateway = ActionGateway(stub, policies=ENABLED_PRIVATE)
        cq = await gateway.invoke(
            "send_private_msg",
            {"user_id": "1", "message": "[CQ:image,file=file:///etc/passwd]"},
            principal="system",
        )
        assert (cq.outcome, cq.gate) == ("rejected", "resource_rejected")
        segments = await gateway.invoke(
            "send_private_msg",
            {
                "user_id": "1",
                "message": [
                    {"type": "image", "data": {"file": "/etc/passwd"}}
                ],
            },
            principal="system",
        )
        assert (segments.outcome, segments.gate) == ("rejected", "resource_rejected")
        assert stub.calls == []
        # 普通文本带 https 链接：scheme 提取命中但 validate 放行，不误伤。
        plain = await gateway.invoke(
            "send_private_msg",
            {"user_id": "1", "message": "看这个 https://example.com/a?b=1 不错"},
            principal="system",
        )
        assert plain.outcome == "ok" and len(stub.calls) == 1

    run(scenario())


# ------------------------------------------------------------ 修复 6


def test_shim_empty_platform_response_returns_empty_dict():
    async def scenario():
        stub = StubTransport(response={})
        gateway = ActionGateway(
            stub, policies=ENABLED_PRIVATE, quota=SendQuota()
        )
        client = HttpOneBotClient(gateway=gateway)
        result = await client.send_private_msg("10001", "hi")
        assert result == {}
        assert len(stub.calls) == 1

    run(scenario())


# ------------------------------------------------------------ 修复 7


def test_facade_send_private_with_real_quota_token():
    async def scenario():
        fake = FakeAdapter()
        quota = SendQuota()
        fake.action_gateway.quota = quota
        fake.action_gateway.policies["send_private_msg"] = {"enabled": True}
        token = quota.reserve("10001", 1).consume()
        sent = await fake.actions.messaging.send_private(
            "10001", "你好", delivery_token=token
        )
        # token 绑定 user 10001：identity 派生正确才会被 accept_token 接受。
        assert sent.outcome == "ok", sent
        assert [c["action"] for c in fake.transport.calls] == ["send_private_msg"]
        # 群发送硬关、零调用：N1 口径是门面一律 group_delivery_
        # unavailable；N2 改判后硬关落在 Gateway policy 层——本用例
        # 未开启 send_group_msg policy，故到达 Gateway 后以 disabled
        # 被拒、transport 仍零新增调用（默认关的层级从门面移到
        # policy，零派发的不变量不变）。
        group = await fake.actions.messaging.send_group(
            "20001", "你好", delivery_token=token
        )
        assert (group.outcome, group.gate) == ("rejected", "disabled")
        assert [c["action"] for c in fake.transport.calls] == ["send_private_msg"]

    run(scenario())


# ------------------------------------------------------------ 修复 8


def test_audit_line_sanitizes_forged_action_name(caplog):
    stub = StubTransport()
    gateway = ActionGateway(stub)
    forged = "get_status\nINJECTEDLINE principal=ops_cli"
    with caplog.at_level(logging.INFO, logger="companion.actions"):
        result = run(gateway.invoke(forged, {}, principal="system"))
    assert result.gate == "unknown_action"
    assert "INJECTEDLINE" not in caplog.text
    assert "action=invalid" in caplog.text
