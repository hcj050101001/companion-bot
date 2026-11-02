"""N6: L3 controlled unlock + §3.4 action_levels + final-audit pins.

Plan: docs/../plans 2026-10-31-napcat-full-api-integration §5/§6 N6.

The unlock contract under test (gateway.py module docstring):
- default stays a hard ban for every principal, policies cannot open it;
- the unlock list is a constructor-time snapshot sourced from the ops
  config segment ``actions.l3_unlock`` — no runtime/inbound path exists
  (invoke() has no unlock parameter, the snapshot is immutable);
- an unlocked action still requires the ops_cli principal plus an
  AuthorizationContext whose account scope covers the (always
  account-derived) target; llm_tool et al. stay principal_forbidden;
- every outcome lands on the one-line audit log, and credential return
  values never appear in any log record.
All evidence is Fake/config-level: no real platform, no real L3 call.
"""

import ast
import asyncio
import inspect
import logging
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from companion.adapters.action_registry import ACTION_REGISTRY
from companion.adapters.actions import ActionsBundle
from companion.adapters.base import AdapterCapabilities
from companion.adapters.fake import FakeAdapter, FakeTransport
from companion.adapters.gateway import (
    ACTION_SPECS,
    PRINCIPALS,
    ActionGateway,
    AuthorizationContext,
    build_action_levels,
)
from companion.adapters.onebot import OneBotAdapter
from companion.adapters.transport import TransportError
from companion.config import (
    HOT_RELOADABLE_CONFIG_PATHS,
    L3_SENSITIVE_ACTION_NAMES,
    ActionsConfig,
    load_settings,
)
from companion.tools import HttpOneBotClient

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

SECRET_VALUE = "ptcz_token=SUPERSECRETVALUE; p_skey=ANOTHERSECRET"
OPS = AuthorizationContext(authorizer="ops-alice", account=True)


def run(coro):
    return asyncio.run(coro)


def _gateway(responses=None, **kwargs):
    transport = FakeTransport(responses)
    return ActionGateway(transport, **kwargs), transport


def _invoke(gateway, action, params=None, **gate):
    gate.setdefault("principal", "ops_cli")
    return run(gateway.invoke(action, params or {}, **gate))


# ------------------------------------------------------- default hard ban


def test_default_gateway_hard_bans_all_l3_for_all_principals():
    policies = {name: {"enabled": True} for name in L3_ACTIONS}
    for principal in sorted(PRINCIPALS):
        gateway, transport = _gateway(policies=policies)
        for name in L3_ACTIONS:
            result = _invoke(
                gateway, name, {"domain": "x.qq.com", "cmd": "c", "data": "d"},
                principal=principal,
            )
            assert (result.outcome, result.gate) == ("rejected", "hard_banned"), (
                principal,
                name,
            )
        assert transport.calls == []


def test_l3_ban_precedes_param_validation_and_writes_audit(caplog):
    gateway, transport = _gateway()
    with caplog.at_level(logging.INFO, logger="companion.actions"):
        # Missing the required domain: still hard_banned, never invalid_params.
        result = _invoke(gateway, "get_cookies", {})
    assert result.gate == "hard_banned"
    assert transport.calls == []
    lines = [r.getMessage() for r in caplog.records if r.name == "companion.actions"]
    assert any("get_cookies" in line and "hard_banned" in line for line in lines)


# ------------------------------------------------------------- unlock path


def test_unlocked_ops_cli_with_account_authorization_dispatches():
    gateway, transport = _gateway(
        responses={
            "get_cookies": {
                "status": "ok",
                "retcode": 0,
                "data": {"cookies": SECRET_VALUE, "bkn": 123},
            }
        },
        l3_unlocked={"get_cookies"},
    )
    result = _invoke(
        gateway,
        "get_cookies",
        {"domain": "ptlogin2.qq.com"},
        authorization=OPS,
    )
    assert result.outcome == "ok" and result.gate is None
    # The caller (an ops boundary) receives the data...
    assert result.data == {"cookies": SECRET_VALUE, "bkn": 123}
    assert transport.calls == [
        {
            "method": "post_action",
            "action": "get_cookies",
            "params": {"domain": "ptlogin2.qq.com"},
        }
    ]


def test_unlocked_return_values_never_reach_logs(caplog):
    gateway, _ = _gateway(
        responses={
            "get_cookies": {
                "status": "ok",
                "retcode": 0,
                "data": {"cookies": SECRET_VALUE, "bkn": 123},
            }
        },
        l3_unlocked={"get_cookies"},
    )
    with caplog.at_level(logging.DEBUG):
        result = _invoke(
            gateway,
            "get_cookies",
            {"domain": "ptlogin2.qq.com"},
            authorization=OPS,
        )
    assert result.outcome == "ok"
    assert caplog.records  # the audit line exists
    for record in caplog.records:
        assert SECRET_VALUE not in record.getMessage()
        assert "SUPERSECRETVALUE" not in record.getMessage()
    audit = [
        r.getMessage() for r in caplog.records if r.name == "companion.actions"
    ]
    assert any(
        "get_cookies" in line and "ops-alice" in line and "outcome=ok" in line
        for line in audit
    )


def test_unlocked_tri_state_platform_reject_and_transport_error():
    gateway, _ = _gateway(
        responses={
            "get_csrf_token": {"status": "failed", "retcode": 1200, "data": None}
        },
        l3_unlocked={"get_csrf_token"},
    )
    rejected = _invoke(gateway, "get_csrf_token", {}, authorization=OPS)
    assert rejected.outcome == "rejected" and rejected.error_code == "1200"

    gateway, _ = _gateway(
        responses={"get_csrf_token": TransportError("get_csrf_token", "Timeout")},
        l3_unlocked={"get_csrf_token"},
    )
    ambiguous = _invoke(gateway, "get_csrf_token", {}, authorization=OPS)
    assert ambiguous.outcome == "ambiguous" and ambiguous.error_code == "transport"


def test_unlocked_still_forbids_non_ops_principals():
    gateway, transport = _gateway(l3_unlocked=set(L3_ACTIONS))
    for principal in sorted(PRINCIPALS - {"ops_cli"}):
        result = _invoke(
            gateway,
            "get_cookies",
            {"domain": "x.qq.com"},
            principal=principal,
            authorization=OPS,
        )
        assert (result.outcome, result.gate) == (
            "rejected",
            "principal_forbidden",
        ), principal
    assert transport.calls == []


def test_unlocked_requires_authorization_context_with_account_scope():
    gateway, transport = _gateway(l3_unlocked={"get_cookies"})
    missing = _invoke(gateway, "get_cookies", {"domain": "x.qq.com"})
    assert (missing.outcome, missing.gate) == ("rejected", "authorization_missing")
    blank = _invoke(
        gateway,
        "get_cookies",
        {"domain": "x.qq.com"},
        authorization=AuthorizationContext(authorizer="", account=True),
    )
    assert blank.gate == "authorization_missing"
    no_account = _invoke(
        gateway,
        "get_cookies",
        {"domain": "x.qq.com"},
        authorization=AuthorizationContext(
            authorizer="ops-alice", groups=frozenset({"1"}), account=False
        ),
    )
    assert (no_account.outcome, no_account.gate) == (
        "rejected",
        "authorization_scope",
    )
    assert transport.calls == []


def test_l3_target_cannot_be_downgraded_by_injected_params():
    # Review P3-1: the c3 account target is structural — a group-only
    # context must fail even when the params carry a group_id that a
    # generic target derivation would happily match.
    group_only = AuthorizationContext(
        authorizer="ops-alice", groups=frozenset({"999"}), account=False
    )
    gateway, transport = _gateway(l3_unlocked=set(L3_ACTIONS))
    for name in L3_ACTIONS:
        result = _invoke(
            gateway,
            name,
            {"domain": "x.qq.com", "cmd": "c", "data": "d", "group_id": "999"},
            authorization=group_only,
        )
        assert (result.outcome, result.gate) == (
            "rejected",
            "authorization_scope",
        ), name
    assert transport.calls == []
    # And the account context is unaffected by the same injected keys:
    # authorization still resolves against the account scope.
    ok = _invoke(
        gateway,
        "get_cookies",
        {"domain": "x.qq.com", "group_id": "999"},
        authorization=OPS,
    )
    assert ok.gate != "authorization_scope"


def test_unlock_list_is_per_action_siblings_stay_banned():
    gateway, transport = _gateway(l3_unlocked={"get_cookies"})
    sibling = _invoke(gateway, "get_csrf_token", {}, authorization=OPS)
    assert sibling.gate == "hard_banned"
    listed = _invoke(
        gateway, "get_cookies", {"domain": "x.qq.com"}, authorization=OPS
    )
    assert listed.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["get_cookies"]


def test_policy_can_still_switch_an_unlocked_action_off():
    gateway, transport = _gateway(
        policies={"get_cookies": {"enabled": False}},
        l3_unlocked={"get_cookies"},
    )
    result = _invoke(
        gateway, "get_cookies", {"domain": "x.qq.com"}, authorization=OPS
    )
    assert (result.outcome, result.gate) == ("rejected", "disabled")
    assert transport.calls == []


def test_unlocked_param_rules_apply():
    gateway, transport = _gateway(l3_unlocked={"get_cookies", "send_packet"})
    no_domain = _invoke(gateway, "get_cookies", {}, authorization=OPS)
    assert (no_domain.outcome, no_domain.gate) == ("rejected", "invalid_params")
    no_data = _invoke(gateway, "send_packet", {"cmd": "x"}, authorization=OPS)
    assert (no_data.outcome, no_data.gate) == ("rejected", "invalid_params")
    ok = _invoke(
        gateway, "send_packet", {"cmd": "x", "data": "00"}, authorization=OPS
    )
    assert ok.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["send_packet"]


def test_unlocked_dry_run_simulates_without_dispatch():
    gateway, transport = _gateway(l3_unlocked={"get_cookies"})
    result = _invoke(
        gateway,
        "get_cookies",
        {"domain": "x.qq.com"},
        authorization=OPS,
        dry_run=True,
    )
    assert (result.outcome, result.gate) == ("simulated", "dry_run")
    assert transport.calls == []


def test_unlocked_via_facade_with_gate_authorization():
    transport = FakeTransport()
    gateway = ActionGateway(transport, l3_unlocked={"get_clientkey"})
    bundle = ActionsBundle(gateway, principal="ops_cli")
    result = run(bundle.sensitive.get_clientkey(authorization=OPS))
    assert result.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["get_clientkey"]
    # The same facade on a default gateway stays hard-banned.
    locked = ActionsBundle(ActionGateway(FakeTransport()), principal="ops_cli")
    banned = run(locked.sensitive.get_clientkey(authorization=OPS))
    assert banned.gate == "hard_banned"
    # A non-ops principal behind the facade is forbidden even unlocked.
    as_system = ActionsBundle(gateway, principal="system")
    forbidden = run(as_system.sensitive.get_clientkey(authorization=OPS))
    assert forbidden.gate == "principal_forbidden"


# ------------------------------------------- snapshot / restart semantics


def test_unlock_snapshot_is_immutable_after_construction():
    source = {"get_cookies"}
    gateway, transport = _gateway(l3_unlocked=source)
    source.add("get_csrf_token")
    source.discard("get_cookies")
    assert gateway.l3_unlocked == frozenset({"get_cookies"})
    still_banned = _invoke(gateway, "get_csrf_token", {}, authorization=OPS)
    assert still_banned.gate == "hard_banned"
    still_open = _invoke(
        gateway, "get_cookies", {"domain": "x.qq.com"}, authorization=OPS
    )
    assert still_open.outcome == "ok"
    assert [c["action"] for c in transport.calls] == ["get_cookies"]
    with pytest.raises(AttributeError):
        gateway.l3_unlocked = frozenset({"get_csrf_token"})


def test_no_runtime_unlock_surface():
    # invoke() takes no unlock knob; the constructor rejects non-L3 names.
    assert "unlock" not in " ".join(inspect.signature(ActionGateway.invoke).parameters)
    with pytest.raises(ValueError):
        ActionGateway(FakeTransport(), l3_unlocked={"get_status"})
    with pytest.raises(ValueError):
        ActionGateway(FakeTransport(), l3_unlocked={"not_a_real_action"})


def test_http_client_forwards_unlock_and_rejects_double_ownership():
    client = HttpOneBotClient(l3_unlocked={"get_cookies"})
    assert client.action_gateway.l3_unlocked == frozenset({"get_cookies"})
    default_client = HttpOneBotClient()
    assert default_client.action_gateway.l3_unlocked == frozenset()
    owned = ActionGateway(FakeTransport())
    with pytest.raises(ValueError):
        HttpOneBotClient(gateway=owned, l3_unlocked={"get_cookies"})


# ------------------------------------------------------------ config layer


def test_config_literal_matches_registry_l3_set():
    registry_l3 = {
        e.name
        for e in ACTION_REGISTRY
        if e.level == "L3" and e.disposition == "registered"
    }
    assert set(L3_SENSITIVE_ACTION_NAMES) == registry_l3 == set(L3_ACTIONS)


def test_actions_config_l3_unlock_validation():
    assert ActionsConfig().l3_unlock == ()
    assert ActionsConfig(l3_unlock=["get_cookies"]).l3_unlock == ("get_cookies",)
    with pytest.raises(Exception):
        ActionsConfig(l3_unlock=["get_cookies", "get_cookies"])
    with pytest.raises(Exception):
        ActionsConfig(l3_unlock=["get_status"])
    with pytest.raises(Exception):
        ActionsConfig(l3_unlock=["*"])


def test_l3_unlock_is_not_hot_reloadable():
    # Restart-to-change is structural: the actions.* subtree is absent
    # from the hot-reload whitelist, so a live process never picks up
    # an edited unlock list.
    assert "actions.l3_unlock" not in HOT_RELOADABLE_CONFIG_PATHS
    assert not any(
        path == "actions" or path.startswith("actions.")
        for path in HOT_RELOADABLE_CONFIG_PATHS
    )


def test_ingress_wires_config_l3_unlock_into_gateway(tmp_path, monkeypatch):
    from companion.brain import CompanionBrain
    from companion.ingress import EventProcessor

    monkeypatch.setenv("ONEBOT_SECRET", "n6-test-secret")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    base = load_settings()

    class FakeLLM:
        async def generate(self, _prompt):
            return "ok"

        async def generate_json(self, _prompt):
            return "{}"

    def build(app_extra):
        settings = replace(
            base,
            onebot_secret="n6-test-secret",
            dev=False,
            root=tmp_path,
            app={**base.app, "sqlite_path": "memory.db", **app_extra},
        )
        processor = EventProcessor(
            settings, brain=CompanionBrain(settings.persona, FakeLLM())
        )
        return processor.sender.action_gateway.l3_unlocked

    assert build({}) == frozenset()
    assert build({"actions": {"l3_unlock": ["get_cookies"]}}) == frozenset(
        {"get_cookies"}
    )


# ------------------------------------------------------- §3.4 action_levels


def test_action_levels_cover_exactly_the_spec_set():
    levels = build_action_levels()
    assert levels == {name: True for name in ACTION_SPECS}
    assert FakeAdapter().capabilities.action_levels == levels
    assert OneBotAdapter.capabilities.action_levels == levels
    assert AdapterCapabilities().action_levels == {}


# ------------------------------------------------- convergence: no callers


def test_no_l3_facade_call_sites_outside_the_facade_module():
    root = Path(__file__).resolve().parents[1] / "companion"
    methods = {
        "get_cookies",
        "get_csrf_token",
        "get_credentials",
        "get_clientkey",
        "nc_get_rkey",
        "get_rkey",
        "get_rkey_server",
        "send_packet",
    }
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "actions.py" and path.parent.name == "adapters":
            continue  # the facade definitions themselves
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in methods
                and isinstance(node.ctx, ast.Load)
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
