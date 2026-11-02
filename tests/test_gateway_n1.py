"""N1 ActionGateway contract tests (plan §3.1/§3.2/§5, §6 N1 row).

Everything runs against a stub transport: no network, no live NapCat,
no real sends. Covers spec/registry alignment, principal + L3 gates,
param/resource validation, the dispatched three-state mapping, dry_run
as simulated, operation_id replay protection, redaction of audit /
error paths, the ResourceResolver basics and the SendQuota path.
"""

import asyncio
import logging

import pytest

from companion.adapters.action_registry import ACTION_REGISTRY, REGISTRY_BY_NAME
from companion.adapters.gateway import (
    ACTION_SPECS,
    PRINCIPALS,
    ActionGateway,
    InMemoryOperationLedger,
    SqliteOperationLedger,
    get_action_spec,
    redact_text,
    redact_value,
)
from companion.adapters.resources import ResourceValidationError, validate_resource_ref
from companion.adapters.transport import (
    TransportError,
    extract_platform_message_id,
    receipt_from_result,
)
from companion.quota import SendQuota

L3_ACTIONS = (
    "get_cookies",
    "get_csrf_token",
    "get_credentials",
    "get_clientkey",
    "nc_get_rkey",
    "get_rkey",
    "get_rkey_server",
    "send_packet",
)


class StubTransport:
    """In-memory transport double: records calls, replays a canned outcome."""

    def __init__(self, response=None, error: Exception | None = None):
        self.response = {"status": "ok", "retcode": 0, "data": {}} if response is None else response
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def post_action(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response(action, params)
        return self.response

    async def get_action(self, action):
        self.calls.append((action, {}))
        if self.error is not None:
            raise self.error
        return self.response


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ spec alignment


def test_specs_align_with_registry():
    canonical = [
        e for e in ACTION_REGISTRY if e.disposition == "registered" and e.alias_of is None
    ]
    for entry in canonical:
        spec = get_action_spec(entry.name)
        assert spec is not None, entry.name
        assert spec.name == entry.name
        assert spec.level == entry.level, entry.name
        assert spec.kind == entry.kind, entry.name
        assert spec.tier == entry.tier, entry.name
        assert spec.schema_status == entry.schema, entry.name
        assert spec.default_enabled == entry.default_enabled, entry.name
        assert spec.idempotent is (entry.kind == "query"), entry.name
    # N4 改判（原断言：唯一非登记 spec 为 qzone 旧端点 publish_message
    # 的 legacy 合成项）：publish_message 已按方案 §6 N4 迁移到登记
    # 在册的 send_qzone_msg 并下线合成 spec，ACTION_SPECS 自此与登记
    # 表 canonical 条目严格一致、无任何合成项；qzone 工具面改以
    # policy_key=qzone 调 send_qzone_msg（见 test_group_domain_n4）。
    assert set(ACTION_SPECS) - {e.name for e in canonical} == set()
    assert "publish_message" not in ACTION_SPECS
    qzone_spec = get_action_spec("send_qzone_msg")
    assert qzone_spec is not None and qzone_spec.level == "L1"
    assert "qzone" in qzone_spec.allowed_policy_keys


def test_alias_resolves_to_canonical():
    # N5 定案纠正（非口径漂移）：move/rename 经 bundle schema 证据
    # 改判独立 canonical，别名表随登记表派生、二者不再解析到 trans。
    assert get_action_spec("move_group_file").name == "move_group_file"
    assert get_action_spec("rename_group_file").name == "rename_group_file"
    assert get_action_spec("trans_group_file").name == "trans_group_file"
    assert get_action_spec(".ocr_image").name == "ocr_image"
    assert get_action_spec("get_group_ignore_add_request").name == "get_group_ignored_notifies"
    stub = StubTransport()
    gateway = ActionGateway(stub)
    result = run(gateway.invoke(".ocr_image", {"image": "x"}, principal="system"))
    assert result.outcome == "ok"
    assert stub.calls[0][0] == "ocr_image"  # dispatched under the canonical name


def test_non_registered_actions_have_no_spec():
    for name in ("bot_exit", "set_restart", "set_group_anonymous", "unknown", "get_group_todo"):
        assert REGISTRY_BY_NAME[name].disposition != "registered"
        assert get_action_spec(name) is None, name
    stub = StubTransport()
    gateway = ActionGateway(stub)
    result = run(gateway.invoke("bot_exit", {}, principal="ops_cli"))
    assert (result.outcome, result.gate) == ("rejected", "unknown_action")
    assert stub.calls == []


# ------------------------------------------------------------ gates


def test_unknown_action_rejected_without_transport():
    stub = StubTransport()
    gateway = ActionGateway(stub)
    result = run(gateway.invoke("no_such_action_xyz", {}, principal="system"))
    assert (result.outcome, result.gate) == ("rejected", "unknown_action")
    assert stub.calls == []


def test_l3_hard_banned_for_all_principals_even_when_enabled():
    for action in L3_ACTIONS:
        assert get_action_spec(action).level == "L3", action
        for principal in sorted(PRINCIPALS):
            stub = StubTransport()
            gateway = ActionGateway(stub, policies={action: {"enabled": True}})
            result = run(gateway.invoke(action, {}, principal=principal))
            assert (result.outcome, result.gate) == ("rejected", "hard_banned"), (
                action,
                principal,
            )
            assert stub.calls == []


def test_principal_missing_unknown_and_forbidden():
    stub = StubTransport()
    gateway = ActionGateway(stub)
    missing = run(gateway.invoke("get_status", {}))
    assert (missing.outcome, missing.gate) == ("rejected", "principal_missing")
    unknown = run(gateway.invoke("get_status", {}, principal="intruder"))
    assert (unknown.outcome, unknown.gate) == ("rejected", "principal_unknown")
    # L2 never reaches the LLM tool surface.
    forbidden = run(gateway.invoke("set_group_kick", {"group_id": "1"}, principal="llm_tool"))
    assert (forbidden.outcome, forbidden.gate) == ("rejected", "principal_forbidden")
    assert stub.calls == []


def test_param_validation_target_mismatch():
    stub = StubTransport()
    gateway = ActionGateway(stub, policies={"send_private_msg": {"enabled": True}})
    wrong_target = run(
        gateway.invoke(
            "send_private_msg",
            {"user_id": "1", "message": "hi", "group_id": "9"},
            principal="system",
        )
    )
    assert (wrong_target.outcome, wrong_target.gate) == ("rejected", "invalid_params")
    missing = run(gateway.invoke("get_group_info", {}, principal="system"))
    assert (missing.outcome, missing.gate) == ("rejected", "invalid_params")
    assert stub.calls == []


# ------------------------------------------------------------ dispatch states


def test_l0_three_states_and_data_passthrough():
    data = {"group_id": 123, "group_name": "demo"}
    ok_gateway = ActionGateway(StubTransport({"status": "ok", "retcode": 0, "data": data}))
    result = run(ok_gateway.invoke("get_group_info", {"group_id": "123"}, principal="system"))
    assert result.outcome == "ok" and result.data == data and result.gate is None

    failed_gateway = ActionGateway(
        StubTransport({"status": "failed", "retcode": 1200, "data": None})
    )
    result = run(
        failed_gateway.invoke("get_group_info", {"group_id": "123"}, principal="system")
    )
    assert result.outcome == "rejected" and result.error_code == "1200"

    error_gateway = ActionGateway(
        StubTransport(error=TransportError("get_group_info", "ConnectError"))
    )
    result = run(error_gateway.invoke("get_group_info", {"group_id": "123"}, principal="system"))
    assert result.outcome == "ambiguous" and result.error_code == "transport"


def test_dry_run_is_simulated_never_ok():
    stub = StubTransport()
    gateway = ActionGateway(stub)
    result = run(
        gateway.invoke("get_group_info", {"group_id": "1"}, principal="system", dry_run=True)
    )
    assert result.outcome == "simulated" and result.outcome != "ok"
    assert result.gate == "dry_run"
    assert stub.calls == []


def test_operation_id_replay_protection():
    async def scenario():
        stub = StubTransport({"status": "ok", "retcode": 0, "data": {"message_id": 7}})
        ledger = InMemoryOperationLedger()
        gateway = ActionGateway(
            stub,
            policies={"send_private_msg": {"enabled": True}},
            operation_ledger=ledger,
        )
        params = {"user_id": "1", "message": "hi"}
        first = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-done"
        )
        assert first.outcome == "ok" and len(stub.calls) == 1
        replay = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-done"
        )
        assert replay.outcome == "ok" and replay.gate is None and replay.raw is None
        assert replay.operation_id == "op-done" and len(stub.calls) == 1

        stub.error = TransportError("send_private_msg", "TimeoutException")
        ambiguous = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-amb"
        )
        assert ambiguous.outcome == "ambiguous" and len(stub.calls) == 2
        blocked = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-amb"
        )
        assert (blocked.outcome, blocked.gate) == ("rejected", "replay_blocked")
        assert len(stub.calls) == 2

    run(scenario())


def test_operation_id_sqlite_ledger_roundtrip(tmp_path):
    async def scenario():
        stub = StubTransport({"status": "ok", "retcode": 0, "data": {"message_id": 9}})
        ledger = SqliteOperationLedger(tmp_path / "ops.db")
        gateway = ActionGateway(
            stub,
            policies={"send_private_msg": {"enabled": True}},
            operation_ledger=ledger,
        )
        params = {"user_id": "1", "message": "hi"}
        first = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-sql"
        )
        assert first.outcome == "ok"
        record = ledger.lookup("op-sql")
        assert record is not None and record.state == "done" and record.outcome == "ok"
        replay = await gateway.invoke(
            "send_private_msg", params, principal="system", operation_id="op-sql"
        )
        assert replay.outcome == "ok" and len(stub.calls) == 1
        ledger.close()

    run(scenario())


# ------------------------------------------------------------ redaction


def test_audit_log_and_error_code_are_redacted(caplog):
    secret = "SECRET123"
    stub = StubTransport(
        {"status": "ok", "retcode": 0, "data": {"url": f"https://cdn.example/f?sign={secret}"}}
    )
    gateway = ActionGateway(stub)
    with caplog.at_level(logging.INFO, logger="companion.actions"):
        result = run(
            gateway.invoke(
                "check_url_safely",
                {"url": f"https://example.com/x?token={secret}"},
                principal="system",
            )
        )
    assert result.outcome == "ok"
    assert secret not in caplog.text

    failing = ActionGateway(
        StubTransport({"status": "failed", "retcode": f"https://example/cb?token={secret}&x=1"})
    )
    result = run(
        failing.invoke(
            "check_url_safely", {"url": "https://example.com/x"}, principal="system"
        )
    )
    assert result.outcome == "rejected"
    assert secret not in (result.error_code or "")
    assert len(result.error_code or "") <= 64


def test_redact_helpers():
    redacted = redact_text("https://a.b/c?token=xyz&sign=abc and token=plainsecret")
    assert "xyz" not in redacted and "abc" not in redacted and "plainsecret" not in redacted
    value = redact_value(
        {"token": "abc", "nested": [{"url": "https://a.b/?sign=zzz"}], "keep": "plain"}
    )
    assert value["token"] == "<redacted>"
    assert "zzz" not in value["nested"][0]["url"]
    assert value["keep"] == "plain"


# ------------------------------------------------------------ resources


def test_resource_ref_rules(tmp_path):
    with pytest.raises(ResourceValidationError):
        validate_resource_ref("file:///etc/passwd")
    with pytest.raises(ResourceValidationError):
        validate_resource_ref("/etc/passwd", allowed_roots=(tmp_path,))
    with pytest.raises(ResourceValidationError):
        validate_resource_ref("/etc/passwd")  # no roots: all absolute paths rejected
    inside = str(tmp_path / "a.txt")
    assert validate_resource_ref(inside, allowed_roots=(tmp_path,)) == inside
    assert validate_resource_ref("HTTPS://example.com/f?x=1") == "HTTPS://example.com/f?x=1"
    assert validate_resource_ref("plain-file-id-123") == "plain-file-id-123"


def test_gateway_rejects_local_file_ref():
    stub = StubTransport()
    gateway = ActionGateway(stub)
    result = run(gateway.invoke("get_image", {"file": "file:///etc/passwd"}, principal="system"))
    assert (result.outcome, result.gate) == ("rejected", "resource_rejected")
    assert stub.calls == []


# ------------------------------------------------------------ transport helpers


def test_extract_platform_message_id_semantics():
    assert extract_platform_message_id({"data": {"message_id": -123}}) == "-123"
    assert extract_platform_message_id({"data": {"message_id": 42}, "message_id": 9}) == "42"
    assert extract_platform_message_id({"data": {"message_id": True}, "message_id": 9}) == "9"
    assert extract_platform_message_id({"data": {"message_id": 2**31}}) is None
    assert extract_platform_message_id({"data": {}}) is None
    assert extract_platform_message_id("junk") is None


def test_receipt_from_result_mapping():
    assert receipt_from_result(None).outcome == "accepted"
    ok = receipt_from_result({"status": "ok", "data": {"message_id": 42}})
    assert ok.outcome == "accepted" and ok.platform_message_id == "42"
    assert receipt_from_result({"status": "dry_run"}).outcome == "accepted"
    assert receipt_from_result({"status": "busy"}).outcome == "rejected"
    assert receipt_from_result({"status": "failed"}).outcome == "ambiguous"
    assert receipt_from_result({"status": 3}).outcome == "ambiguous"
    assert receipt_from_result(["junk"]).outcome == "ambiguous"


def test_transport_error_message_has_no_payload():
    err = TransportError("get_msg", "ConnectError")
    assert "get_msg" in str(err) and "ConnectError" in str(err)
    assert "http" not in str(err)


# ------------------------------------------------------------ quota path


def test_quota_reserve_path_and_token_rejection():
    async def scenario():
        stub = StubTransport({"status": "ok", "retcode": 0, "data": {"message_id": 5}})
        quota = SendQuota()
        policies = {"send_private_msg": {"enabled": True}}
        gateway = ActionGateway(stub, policies=policies, quota=quota)
        assert gateway.policies is policies
        params = {"user_id": "10001", "message": "hi"}
        dispatched = await gateway.invoke(
            "send_private_msg", params, principal="system", identity="10001"
        )
        assert dispatched.outcome == "ok" and len(stub.calls) == 1

        other = SendQuota()
        foreign_token = other.reserve("10001", 1).consume()
        rejected = await gateway.invoke(
            "send_private_msg",
            params,
            principal="system",
            identity="10001",
            quota_token=foreign_token,
        )
        assert (rejected.outcome, rejected.gate) == ("rejected", "quota_unavailable")
        assert len(stub.calls) == 1

    run(scenario())
