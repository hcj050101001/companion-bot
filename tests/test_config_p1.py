"""P1 configuration formalization contracts.

The production fixture is a sanitized copy: its only identifier is the
synthetic user ID 10001, and all secret values in this file are synthetic.
"""

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from companion.config import (
    AppConfig,
    ConfigReloader,
    ConfigValidationError,
    EnvironmentSecretProvider,
    load_settings,
    load_yaml_data,
    mapping_config_field_paths,
    model_config_field_paths,
    validate_app_config,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/config_p1/production-sanitized.yaml"
SYNTHETIC_SECRET = "synthetic-onebot-secret-for-p1-tests"


def _flatten(value, prefix=""):
    if isinstance(value, dict) and value:
        leaves = {}
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            leaves.update(_flatten(child, path))
        return leaves
    return {prefix: value}


def _minimal(**overrides):
    raw = {"allowed_private_user_ids": ["10001"]}
    raw.update(overrides)
    return raw


def test_example_field_set_matches_model_field_set():
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert mapping_config_field_paths(example) == model_config_field_paths(AppConfig)


def test_example_loads_after_replacing_placeholder_ids():
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    example["allowed_private_user_ids"] = ["10001"]
    config = validate_app_config(example)
    assert config.tools.send_private_msg.rate_limit == 18
    assert config.read_only_tools.policies.web_search.rate_limit == 6


def test_minimal_config_gets_formal_defaults():
    config = validate_app_config(_minimal())
    assert config.requests_per_minute == 6
    assert config.napcat_base_url == "http://127.0.0.1:3000"
    assert config.llm.provider == "deepseek"
    assert config.tools.alert_private_msg.rate_limit == 2
    assert config.proactive_care.enabled is False


@pytest.mark.parametrize(
    ("override", "key_path"),
    [
        ({"unknown_key": 1}, "unknown_key"),
        ({"requests_per_minute": "6"}, "requests_per_minute"),
        ({"llm": {"provider": "not-a-provider"}}, "llm.provider"),
        (
            {"read_only_tools": {"policies": {"current_time": {"enabled": "yes"}}}},
            "read_only_tools.policies.current_time.enabled",
        ),
        ({"response_delay_seconds": {"min": 5, "max": 4}}, "response_delay_seconds"),
        ({"allowed_private_user_ids": []}, "allowed_private_user_ids"),
    ],
)
def test_invalid_config_fails_fast_and_names_the_key(override, key_path):
    with pytest.raises(ConfigValidationError) as raised:
        validate_app_config(_minimal(**override))
    assert key_path in str(raised.value)


def test_care_known_user_ids_is_a_compatible_alias():
    config = validate_app_config({"care_known_user_ids": [10001]})
    assert config.allowed_private_user_ids == ("10001",)
    assert config.care_known_user_ids == ["10001"]


def test_care_alias_must_match_legacy_key_when_both_are_set():
    config = validate_app_config(
        {
            "allowed_private_user_ids": ["10001"],
            "care_known_user_ids": [10001],
        }
    )
    assert config.care_known_user_ids == ["10001"]
    with pytest.raises(ConfigValidationError) as raised:
        validate_app_config(
            {
                "allowed_private_user_ids": ["10001"],
                "care_known_user_ids": ["10002"],
            }
        )
    assert "care_known_user_ids" in str(raised.value)
    assert "10002" not in str(raised.value)


def test_production_sanitized_copy_matches_formal_model_leaf_by_leaf():
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    config = validate_app_config(raw)
    model_leaves = _flatten(config.model_dump(mode="python"))
    for path, value in _flatten(raw).items():
        # The validated ID list is stored as a tuple (immutability);
        # compare sequence leaves element-wise.
        expected = tuple(value) if isinstance(value, list) else value
        assert model_leaves[path] == expected, path
    # Production omits these policy numbers; the old consumer defaults
    # (ToolGateway: 6 per 60 seconds) are now the formal model defaults.
    for name in ("current_time", "weather_lookup", "web_search"):
        policy = getattr(config.read_only_tools.policies, name)
        assert policy.rate_limit == 6
        assert policy.window_seconds == 60
    assert config.read_only_tools.enabled is True
    assert config.connection_health.enabled is True
    assert config.proactive_care.enabled is False


def test_production_sanitized_copy_loads_with_legacy_view_intact(tmp_path, monkeypatch):
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    raw["safety_file"] = str(ROOT / "config/safety.yaml")
    raw["persona_file"] = str(ROOT / "config/persona.md")
    raw["persona_profile_file"] = str(ROOT / "config/persona.yaml")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONEBOT_SECRET", SYNTHETIC_SECRET)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.delenv("NAPCAT_BASE_URL", raising=False)

    settings = load_settings()

    assert settings.config == validate_app_config(raw)
    assert settings.app["allowed_private_user_ids"] == ["10001"]
    assert settings.app["care_known_user_ids"] == ["10001"]
    for path, value in _flatten(raw).items():
        if path in {"safety_file", "persona_file", "persona_profile_file"}:
            continue
        loaded = settings.app
        for part in path.split("."):
            loaded = loaded[part]
        assert loaded == value, path
    # The legacy dict still only carries explicitly configured policy
    # keys; omitted numbers remain the consumers' documented defaults.
    current_time = settings.app["read_only_tools"]["policies"]["current_time"]
    assert "rate_limit" not in current_time
    assert settings.config.read_only_tools.policies.current_time.rate_limit == 6


def test_settings_and_provider_repr_do_not_expose_the_onebot_secret(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"allowed_private_user_ids": ["10001"]}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONEBOT_SECRET", SYNTHETIC_SECRET)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    settings = load_settings()
    assert SYNTHETIC_SECRET not in repr(settings)
    assert SYNTHETIC_SECRET not in repr(settings.secret_provider)
    assert SYNTHETIC_SECRET not in repr(settings.config)


def test_validation_error_names_a_secret_like_key_without_its_value():
    raw = _minimal(deepseek_api_key="synthetic-secret-value-must-not-leak")
    with pytest.raises(ConfigValidationError) as raised:
        validate_app_config(raw)
    message = str(raised.value)
    assert "deepseek_api_key" in message
    assert "synthetic-secret-value-must-not-leak" not in message


def test_environment_secret_provider_is_name_scoped_and_redacted():
    provider = EnvironmentSecretProvider({"ONEBOT_SECRET": SYNTHETIC_SECRET})
    assert provider.get("ONEBOT_SECRET") == SYNTHETIC_SECRET
    assert SYNTHETIC_SECRET not in repr(provider)
    with pytest.raises(ValueError, match="unsupported secret name"):
        provider.get("NOT_A_SECRET")


def _write_reloader_config(path, **overrides):
    raw = _minimal(**overrides)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_config_reloader_accepts_whitelisted_switch_and_writes_audit(tmp_path):
    path = tmp_path / "config.yaml"
    audit_path = tmp_path / "audit/config.jsonl"
    _write_reloader_config(path, proactive_care={"enabled": False})
    reloader = ConfigReloader.load(path, audit_path=audit_path, actor="p1-test")
    _write_reloader_config(path, proactive_care={"enabled": True})
    assert reloader.reload_if_changed() == ("proactive_care.enabled",)
    assert reloader.config.proactive_care.enabled is True
    record = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert record["changed_paths"] == ["proactive_care.enabled"]
    assert record["result_code"] == "ok"
    assert "True" not in audit_path.read_text(encoding="utf-8")


def test_config_reloader_rejects_non_whitelisted_change_and_writes_audit(tmp_path):
    path = tmp_path / "config.yaml"
    audit_path = tmp_path / "audit/config.jsonl"
    _write_reloader_config(path, requests_per_minute=6)
    reloader = ConfigReloader.load(path, audit_path=audit_path, actor="p1-test")
    _write_reloader_config(path, requests_per_minute=7)
    with pytest.raises(ConfigValidationError) as raised:
        reloader.reload_if_changed()
    assert "requests_per_minute" in str(raised.value)
    assert "7" not in str(raised.value)
    assert reloader.config.requests_per_minute == 6
    record = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert record["changed_paths"] == ["requests_per_minute"]
    assert record["result_code"] == "rejected"


def test_app_config_deepcopy_of_fixture_has_no_drift():
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert validate_app_config(deepcopy(raw)) == validate_app_config(raw)


@pytest.mark.parametrize("has_process_value", [False, True])
def test_openai_compatible_key_in_dotenv_is_ignored(
    tmp_path, monkeypatch, caplog, has_process_value
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONEBOT_SECRET", SYNTHETIC_SECRET)
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("NAPCAT_BASE_URL", raising=False)
    if has_process_value:
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "process-value")
    else:
        monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "OPENAI_COMPATIBLE_API_KEY=<redacted>", encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        "allowed_private_user_ids: ['10001']\n", encoding="utf-8"
    )
    load_settings()
    assert os.getenv("OPENAI_COMPATIBLE_API_KEY") == (
        "process-value" if has_process_value else None
    )
    assert "OPENAI_COMPATIBLE_API_KEY" in caplog.text
    assert "dotenv-value" not in caplog.text


def test_yaml_tagged_scalar_error_names_key_without_value(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "allowed_private_user_ids: ['10001']\n"
        "requests_per_minute: !!int PRIVATECONFIGVALUE\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONEBOT_SECRET", SYNTHETIC_SECRET)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    with pytest.raises(ConfigValidationError) as raised:
        load_settings()
    message = str(raised.value)
    assert "requests_per_minute" in message
    assert "PRIVATECONFIGVALUE" not in message


def test_duplicate_yaml_keys_are_rejected_with_their_path(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "allowed_private_user_ids: ['10001']\n"
        "llm:\n  provider: deepseek\n  provider: deepseek\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONEBOT_SECRET", SYNTHETIC_SECRET)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    with pytest.raises(ConfigValidationError) as raised:
        load_settings()
    assert "llm.provider" in str(raised.value)
    assert "duplicate" in str(raised.value)


def test_config_reloader_audits_schema_invalid_candidate(tmp_path):
    path = tmp_path / "config.yaml"
    audit_path = tmp_path / "audit/config.jsonl"
    _write_reloader_config(path, requests_per_minute=6)
    reloader = ConfigReloader.load(path, audit_path=audit_path, actor="p1-test")
    _write_reloader_config(path, requests_per_minute="six")
    with pytest.raises(ConfigValidationError):
        reloader.reload_if_changed()
    assert reloader.config.requests_per_minute == 6
    record = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert record["result_code"] == "rejected"
    assert record["failure_paths"] == ["requests_per_minute"]
    assert "six" not in audit_path.read_text(encoding="utf-8")


def test_config_reloader_audits_malformed_yaml_candidate(tmp_path):
    path = tmp_path / "config.yaml"
    audit_path = tmp_path / "audit/config.jsonl"
    _write_reloader_config(path, requests_per_minute=6)
    reloader = ConfigReloader.load(path, audit_path=audit_path, actor="p1-test")
    path.write_text(
        "allowed_private_user_ids: ['10001']\n"
        "requests_per_minute: !!int PRIVATECONFIGVALUE\n",
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(ConfigValidationError):
        reloader.reload_if_changed()
    assert reloader.config.requests_per_minute == 6
    record = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert record["result_code"] == "rejected"
    assert record["failure_paths"] == ["requests_per_minute"]
    assert "PRIVATECONFIGVALUE" not in audit_path.read_text(encoding="utf-8")


def test_pairs_collection_tag_is_rejected_without_leaking_its_keys():
    source = (
        "allowed_private_user_ids: !!pairs\n"
        "  - PRIVATECONFIGVALUE: !!int broken\n"
    )
    with pytest.raises(ConfigValidationError) as raised:
        validate_app_config(load_yaml_data(source))
    message = str(raised.value)
    assert "allowed_private_user_ids" in message
    assert "PRIVATECONFIGVALUE" not in message


def test_set_collection_tag_is_rejected_without_leaking_its_members():
    source = (
        "allowed_private_user_ids: !!set\n"
        "  PRIVATECONFIGVALUE: null\n"
        "  PRIVATECONFIGVALUE: null\n"
    )
    with pytest.raises(ConfigValidationError) as raised:
        validate_app_config(load_yaml_data(source))
    message = str(raised.value)
    assert "allowed_private_user_ids" in message
    assert "PRIVATECONFIGVALUE" not in message


def test_recursive_alias_is_a_sanitized_config_error(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "allowed_private_user_ids: &ids [*ids]\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONEBOT_SECRET", SYNTHETIC_SECRET)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    with pytest.raises(ConfigValidationError) as raised:
        load_settings()
    assert "allowed_private_user_ids" in str(raised.value)
    assert "recursive" in str(raised.value)


def test_config_reloader_audits_recursive_alias_candidate(tmp_path):
    path = tmp_path / "config.yaml"
    audit_path = tmp_path / "audit/config.jsonl"
    _write_reloader_config(path)
    reloader = ConfigReloader.load(path, audit_path=audit_path, actor="p1-test")
    path.write_text("allowed_private_user_ids: &ids [*ids]\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(ConfigValidationError):
        reloader.reload_if_changed()
    assert reloader.config.allowed_private_user_ids == ("10001",)
    record = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert record["result_code"] == "rejected"
    # The cycle is reported at the self-reference position inside the list.
    assert record["failure_paths"] == ["allowed_private_user_ids[0]"]


def test_config_reloader_pairs_candidate_does_not_leak_into_audit(tmp_path):
    path = tmp_path / "config.yaml"
    audit_path = tmp_path / "audit/config.jsonl"
    _write_reloader_config(path)
    reloader = ConfigReloader.load(path, audit_path=audit_path, actor="p1-test")
    path.write_text(
        "allowed_private_user_ids: !!pairs\n"
        "  - PRIVATECONFIGVALUE: !!int broken\n",
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(ConfigValidationError):
        reloader.reload_if_changed()
    audit_text = audit_path.read_text(encoding="utf-8")
    record = json.loads(audit_text.strip())
    assert record["result_code"] == "rejected"
    assert record["failure_paths"] == ["allowed_private_user_ids"]
    assert "PRIVATECONFIGVALUE" not in audit_text


def test_alert_defaults_match_legacy_consumer_semantics():
    # Legacy consumers (ingress policy construction + ToolGateway):
    # an omitted alert policy means enabled defaults to True before the
    # ALERT_QQ env gate is ANDed in, and dry_run defaults to False.
    # The formal model must describe the same effective defaults; the
    # production fixture is unaffected because it sets both explicitly.
    config = validate_app_config(_minimal())
    alert = config.tools.alert_private_msg
    assert alert.enabled is True
    assert alert.dry_run is False
    assert alert.rate_limit == 2
    assert alert.window_seconds == 60
    partial = validate_app_config(
        _minimal(tools={"alert_private_msg": {"enabled": True}})
    )
    assert partial.tools.alert_private_msg.dry_run is False


# Example values that intentionally differ from the model defaults.
# Anything not listed here must match, so a silent default change in
# the model (or an example edit) turns this test red.
_EXAMPLE_DEFAULT_OVERRIDES = {
    # The example documents the deployed display name; the model
    # default is "unset" (consumers fall back to the provider name).
    "llm.display_name",
    # The example ships the conservative choice (alerts off, dry-run)
    # while the model defaults mirror the legacy consumers; actual
    # alert sending is additionally gated by ALERT_QQ at runtime.
    "tools.alert_private_msg.enabled",
    "tools.alert_private_msg.dry_run",
}


def test_example_values_match_model_defaults_except_registered_overrides():
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    example["allowed_private_user_ids"] = ["10001"]
    example_leaves = _flatten(validate_app_config(example).model_dump(mode="python"))
    minimal_leaves = _flatten(validate_app_config(_minimal()).model_dump(mode="python"))
    assert set(example_leaves) == set(minimal_leaves)
    differing = {
        path
        for path in example_leaves
        if example_leaves[path] != minimal_leaves[path]
    }
    assert differing == _EXAMPLE_DEFAULT_OVERRIDES


@pytest.mark.parametrize(
    ("source", "safe_path"),
    [
        (
            "DEEPSEEK_API_KEY:\n  SYNTHETIC_PRIVATE_VALUE: !!int broken\n",
            "DEEPSEEK_API_KEY",
        ),
        (
            "unknown_section:\n"
            "  SYNTHETIC_PRIVATE_VALUE: 1\n"
            "  SYNTHETIC_PRIVATE_VALUE: 2\n"
            "allowed_private_user_ids: ['10001']\n",
            "unknown_section",
        ),
        (
            "allowed_private_user_ids: ['10001']\n"
            "llm:\n  bogus:\n    SYNTHETIC_PRIVATE_VALUE: !!int broken\n",
            "llm.bogus",
        ),
    ],
)
def test_error_paths_stop_at_unknown_fields_without_leaking_nested_keys(
    source, safe_path
):
    # Keys below an unknown field are data, not configuration field
    # names: they must not be stitched into error paths (which also
    # feed the hot-reload rejected audit).
    with pytest.raises(ConfigValidationError) as raised:
        load_yaml_data(source)
    message = str(raised.value)
    assert safe_path in message
    assert "SYNTHETIC_PRIVATE_VALUE" not in message
    assert all("SYNTHETIC_PRIVATE_VALUE" not in p for p in raised.value.paths)


def test_reloader_audit_paths_do_not_leak_keys_below_unknown_fields(tmp_path):
    path = tmp_path / "config.yaml"
    audit_path = tmp_path / "audit/config.jsonl"
    _write_reloader_config(path)
    reloader = ConfigReloader.load(path, audit_path=audit_path, actor="p1-test")
    path.write_text(
        "allowed_private_user_ids: ['10001']\n"
        "unknown_section:\n"
        "  SYNTHETIC_PRIVATE_VALUE: 1\n"
        "  SYNTHETIC_PRIVATE_VALUE: 2\n",
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(ConfigValidationError):
        reloader.reload_if_changed()
    audit_text = audit_path.read_text(encoding="utf-8")
    record = json.loads(audit_text.strip())
    assert record["result_code"] == "rejected"
    assert record["failure_paths"] == ["unknown_section"]
    assert "SYNTHETIC_PRIVATE_VALUE" not in audit_text


def test_validated_user_ids_cannot_be_mutated_in_place():
    config = validate_app_config(_minimal())
    with pytest.raises(AttributeError):
        config.allowed_private_user_ids.clear()
    with pytest.raises(AttributeError):
        config.allowed_private_user_ids.append("10002")
    assert config.allowed_private_user_ids == ("10001",)
    assert config.care_known_user_ids == ["10001"]


def test_config_reloader_rolls_back_when_success_audit_cannot_be_written(tmp_path):
    path = tmp_path / "config.yaml"
    audit_dir = tmp_path / "audit-as-directory"
    audit_dir.mkdir()
    _write_reloader_config(path, proactive_care={"enabled": False})
    reloader = ConfigReloader.load(path, audit_path=audit_dir, actor="p1-test")
    _write_reloader_config(path, proactive_care={"enabled": True})
    with pytest.raises(OSError):
        reloader.reload_if_changed()
    assert reloader.config.proactive_care.enabled is False
    # The timestamp must not advance: once the audit sink is fixed, the
    # same change is retried instead of being silently unaudited.
    reloader.audit_path = tmp_path / "audit/config.jsonl"
    assert reloader.reload_if_changed() == ("proactive_care.enabled",)
    assert reloader.config.proactive_care.enabled is True
