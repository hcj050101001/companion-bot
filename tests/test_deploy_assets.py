"""Static / rendering tests for the deploy/ assets (P7).

These tests never execute restore-service.sh, never call systemctl, and
never touch the live services. They only render templates, syntax-check
scripts, and compare the rendered units with the out-of-repo runtime
copies when those are present.
"""

from __future__ import annotations

import codecs
import os
import py_compile
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
PROD_BASE = "<deploy-root>"
EXT_RUNTIME = Path(PROD_BASE) / "runtime-24h"
ETC_DROPIN = Path("/etc/systemd/system/qqbot-napcat.service.d/fingerprint.conf")

COMPANION_TMPL = DEPLOY / "systemd" / "qqbot-companion.service.tmpl"
NAPCAT_TMPL = DEPLOY / "systemd" / "qqbot-napcat.service.tmpl"
FINGERPRINT_TMPL = DEPLOY / "systemd" / "qqbot-napcat.service.d" / "fingerprint.conf.tmpl"
TEMPLATES = [COMPANION_TMPL, NAPCAT_TMPL, FINGERPRINT_TMPL]

RESTORE = DEPLOY / "runtime" / "restore-service.sh"
HEALTH = DEPLOY / "bin" / "health-check.sh"
SYNC = DEPLOY / "bin" / "sync-to-runtime.sh"


def render(path: Path) -> str:
    # Default rendering: @RUNTIME@ defaults to @BASE@/runtime-24h.
    return path.read_text().replace("@BASE@", PROD_BASE).replace(
        "@RUNTIME@", f"{PROD_BASE}/runtime-24h"
    )


def render_with(path: Path, base: str, runtime: str) -> str:
    return path.read_text().replace("@BASE@", base).replace("@RUNTIME@", runtime)


def deploy_files(suffix: str) -> list[Path]:
    return sorted(p for p in DEPLOY.rglob(f"*{suffix}") if p.is_file())


# --- template rendering equivalence ---------------------------------------
#
# The golden copies under tests/fixtures/deploy/ are the in-repo snapshot
# of the live units (taken 2026-10-31 from runtime-24h/ and the installed
# /etc drop-in, rendered with the production base). The golden tests run
# unconditionally -- including in CI -- so the equivalence lock cannot be
# silently skipped; refresh the golden files whenever a unit changes on
# purpose. The live comparisons below stay as optional local checks.

GOLDEN = REPO / "tests" / "fixtures" / "deploy"


@pytest.mark.parametrize(
    ("template", "golden_name"),
    [
        (COMPANION_TMPL, "qqbot-companion.service"),
        (NAPCAT_TMPL, "qqbot-napcat.service"),
        (FINGERPRINT_TMPL, "fingerprint.conf"),
    ],
)
def test_template_renders_identical_to_golden(template: Path, golden_name: str) -> None:
    golden = GOLDEN / golden_name
    assert golden.is_file(), f"golden unit missing: {golden}"
    assert render(template) == golden.read_text()


@pytest.mark.parametrize(
    ("template", "runtime_name"),
    [
        (COMPANION_TMPL, "qqbot-companion.service"),
        (NAPCAT_TMPL, "qqbot-napcat.service"),
    ],
)
def test_template_renders_identical_to_runtime_unit(template: Path, runtime_name: str) -> None:
    runtime_unit = EXT_RUNTIME / runtime_name
    if not runtime_unit.is_file():
        pytest.skip(f"out-of-repo runtime unit missing: {runtime_unit}")
    rendered = render(template)
    live = runtime_unit.read_text()
    if runtime_name == "qqbot-companion.service":
        # Known intentional divergence (Codex catch-up review fix): the
        # template raises TimeoutStopSec to 30 to fit the P6 serial
        # shutdown budget; the live unit ran 15 until the 2026-10-31
        # cutover deploy sync aligned the runtime copy to 30 (runbook
        # §5.3). Normalise exactly that one line — either value on
        # either side — so every other drift is still caught.
        for value in ("30", "15"):
            rendered = rendered.replace(
                f"TimeoutStopSec={value}", "TimeoutStopSec=<diverged>"
            )
            live = live.replace(
                f"TimeoutStopSec={value}", "TimeoutStopSec=<diverged>"
            )
    assert rendered == live


def test_fingerprint_conf_template_renders_identical_to_installed_dropin() -> None:
    if not Path(PROD_BASE).is_absolute():
        # Sanitized public tree: PROD_BASE is a placeholder, so a byte
        # comparison against the live installed drop-in (which carries
        # the real production path) is not meaningful.
        pytest.skip("live drop-in comparison requires a real deploy base")
    if not ETC_DROPIN.is_file():
        pytest.skip(f"installed drop-in missing: {ETC_DROPIN}")
    assert render(FINGERPRINT_TMPL) == ETC_DROPIN.read_text()


# --- templates ------------------------------------------------------------


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_uses_placeholder_and_no_hardcoded_base(template: Path) -> None:
    text = template.read_text()
    # Every template now routes its runtime path through @RUNTIME@.
    assert "@RUNTIME@" in text
    assert "runtime-24h" not in text
    assert PROD_BASE not in text
    assert "/home/" + "hatch" not in text


@pytest.mark.parametrize("template", [COMPANION_TMPL, NAPCAT_TMPL], ids=lambda p: p.name)
def test_service_templates_keep_base_placeholder(template: Path) -> None:
    assert "@BASE@" in template.read_text()


def test_default_rendering_has_no_env_override_lines() -> None:
    # Environment= path lines are injected only for non-default
    # credentials / fingerprint paths; the default rendering (which
    # must stay byte-identical to the golden units) has none.
    for template in TEMPLATES:
        assert "Environment=QQBOT_" not in render(template)


def test_companion_template_preserves_directives() -> None:
    text = COMPANION_TMPL.read_text()
    for needle in (
        "WorkingDirectory=@BASE@",
        "Restart=always",
        "RestartSec=0.1",
        "UMask=0077",
        # 30s: post-P6 shutdown is stop-scheduler (10s) + drain (10s)
        # + cleanup, serially.
        "TimeoutStopSec=30",
        "WantedBy=multi-user.target",
    ):
        assert needle in text


def test_napcat_template_preserves_directives() -> None:
    text = NAPCAT_TMPL.read_text()
    for needle in (
        "WorkingDirectory=@BASE@",
        "Restart=always",
        "RestartSec=0.1",
        "UMask=0077",
        "KillMode=process",
        "WantedBy=default.target multi-user.target",
    ):
        assert needle in text


def test_scripts_have_no_hardcoded_base_outside_defaults() -> None:
    scripts = deploy_files(".sh") + deploy_files(".py")
    assert scripts
    for path in scripts:
        text = path.read_text()
        for line in text.splitlines():
            if PROD_BASE not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # comments documenting the defaults are fine
            # Allowed forms only: an env-var default, or the single
            # documented fallback inside the location-derivation helper
            # (a file qualifies only if it actually derives its base
            # from its own runtime-24h location).
            assert (
                "${QQBOT_BASE:-" in line
                or 'os.environ.get("QQBOT_BASE"' in line
                or (
                    "runtime-24h" in text
                    and stripped.startswith(("return Path(", "base=$HOME"))
                )
            ), f"{path}: hardcoded base outside a default: {line}"


# --- syntax ----------------------------------------------------------------


@pytest.mark.parametrize("script", deploy_files(".sh"), ids=lambda p: p.name)
def test_shell_scripts_parse(script: Path) -> None:
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", deploy_files(".py"), ids=lambda p: p.name)
def test_python_scripts_compile(module: Path, tmp_path: Path) -> None:
    py_compile.compile(str(module), cfile=str(tmp_path / "out.pyc"), doraise=True)


# --- restore-service.sh safety anchors --------------------------------------


def test_restore_script_keeps_safety_anchors() -> None:
    text = RESTORE.read_text()
    for needle in (
        "--check",
        "Fingerprint inputs, account format, and namespace capability validated; "
        "no changes applied.",
        "A managed service is active; refusing restore.",
        "QQ is running; refusing restore.",
        "A service port is occupied; refusing restore.",
        "Run as root.",
        "systemctl enable --now qqbot-napcat.service qqbot-companion.service",
        "systemctl daemon-reload",
        "unshare --mount --uts -- true",
        # Stop gate reads ActiveState explicitly (item: transitional
        # states and query failures refuse, not just non-zero exits).
        "systemctl show -p ActiveState --value",
        "could not be determined; refusing restore.",
        "inactive|failed",
    ):
        assert needle in text
    # The old is-active gate must be gone: is-active exits non-zero
    # for deactivating units, which the old gate let slip through.
    assert "systemctl is-active" not in text


def test_restore_script_env_overrides_and_rendered_companion_install() -> None:
    text = RESTORE.read_text()
    for needle in (
        "QQBOT_BASE",
        "QQBOT_RUNTIME",
        "QQBOT_SYSTEMD_DIR",
        "QQBOT_FINGERPRINT_DIR",
        "../systemd",
        "install -m 0644",
        # The fingerprint validation block must survive verbatim.
        "rot_13",
        "machine-info",
        # Review fixes: activation guard for isolated systemd dirs,
        # single-mode unit sources, PYTHONOPTIMIZE-proof validation.
        "NOT activated",
        "Partial template tree",
        "env -u PYTHONOPTIMIZE",
        "companion-launch.py",
        "napcat-watchdog.py",
    ):
        assert needle in text
    # Intentional difference: no symlink install of the companion unit.
    assert "ln -sfn" not in text


def test_restore_runtime_placeholder_env_wiring_and_dropin_guard() -> None:
    text = RESTORE.read_text()
    for needle in (
        # @RUNTIME@ is rendered with the actual runtime path, and the
        # runtime-mode guard follows the actual runtime, not a
        # hardcoded $base/runtime-24h.
        "s|@RUNTIME@|",
        '"$runtime/"',
        # Path-only Environment= injection for non-default paths.
        "Environment=QQBOT_CREDENTIALS_FILE=",
        "Environment=QQBOT_FINGERPRINT_DIR=",
        "default_credentials",
        "default_fingerprint",
        # --check asserts rendered paths match the validated paths.
        "assert_rendered_paths",
        "Rendered companion unit does not reference the validated runtime",
        # Drop-in guard: the runtime fingerprint.conf is never copied
        # unverified; it must match the expected drop-in byte-for-byte.
        "does not match the expected fingerprint drop-in; refusing.",
        "print_expected_fingerprint_conf",
    ):
        assert needle in text
    assert 'cat "$runtime/fingerprint.conf"' not in text
    assert '"$base/runtime-24h/"' not in text


# --- runtime python scripts ---------------------------------------------------


def test_companion_launch_env_overrides_and_guards() -> None:
    text = (DEPLOY / "runtime" / "companion-launch.py").read_text()
    for needle in (
        "QQBOT_BASE",
        "QQBOT_CREDENTIALS_FILE",
        "Required companion credentials are missing",
        "Local proxy bypass is incomplete",
        "127.0.0.1",
        "8090",
    ):
        assert needle in text


def test_watchdog_and_namespace_use_env_base() -> None:
    watchdog = (DEPLOY / "runtime" / "napcat-watchdog.py").read_text()
    namespace = (DEPLOY / "runtime" / "fingerprint-namespace.sh").read_text()
    assert "QQBOT_BASE" in watchdog
    assert "QQBOT_BASE" in namespace
    assert "QQBOT_RUNTIME" in namespace


# --- health-check.sh ----------------------------------------------------------


def test_health_check_is_read_only_and_covers_all_probes() -> None:
    text = HEALTH.read_text()
    assert not re.search(r"systemctl\s+(start|stop|restart|enable|disable|kill)", text)
    for needle in (
        "is-active",
        "3000",
        "6099",
        "8090",
        "/health/ready",
        "/get_status",
        "PASS",
        "FAIL",
        "--max-time 3",
        "QQBOT_ONEBOT_PORT",
        "QQBOT_COMPANION_PORT",
        # -q as the first curl argument disables ~/.curlrc, keeping
        # this check read-only (no output=/location surprises).
        "curl -q -sS --max-time 3",
    ):
        assert needle in text


# --- sync-to-runtime.sh -------------------------------------------------------


def test_sync_script_is_dry_run_first_and_protects_exclusions() -> None:
    text = SYNC.read_text()
    for needle in (
        "--apply",
        "Dry run",
        "pre-deploy-sync-",
        "credentials",
        "*.log",
        "fingerprint",
        # Runtime normalisation + fingerprint-overlap refusal, symlink
        # skip and resolved-path re-check, @RUNTIME@ / env rendering.
        "realpath -m",
        "fingerprint_dir_canonical",
        "SKIP (symlink)",
        "SKIP (excluded resolved path)",
        "s|@RUNTIME@|",
        "Environment=${var}=",
        "QQBOT_CREDENTIALS_FILE",
        "QQBOT_FINGERPRINT_DIR",
    ):
        assert needle in text


# --- sync-to-runtime.sh functional (isolated tmp trees only) -------------------


def run_sync(
    env_extra: dict[str, str], *, apply: bool = False
) -> subprocess.CompletedProcess[str]:
    env_extra = dict(env_extra)
    env_extra.pop("_apply", None)
    env = {**os.environ, **env_extra}
    args = ["bash", str(SYNC)] + (["--apply"] if apply else [])
    return subprocess.run(args, capture_output=True, text=True, env=env)


def test_sync_apply_nondefault_paths_render_env_lines(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    credentials = tmp_path / "alt" / "creds.json"
    fingerprint = tmp_path / "alt" / "fp"
    result = run_sync(
        {
            "QQBOT_RUNTIME": str(runtime),
            "QQBOT_CREDENTIALS_FILE": str(credentials),
            "QQBOT_FINGERPRINT_DIR": str(fingerprint),
        },
        apply=True,
    )
    assert result.returncode == 0, result.stderr
    companion = (runtime / "qqbot-companion.service").read_text()
    napcat = (runtime / "qqbot-napcat.service").read_text()
    dropin = (runtime / "fingerprint.conf").read_text()
    # Actual runtime path is rendered through @RUNTIME@.
    assert f"ExecStart=/usr/bin/python3 {runtime}/companion-launch.py" in companion
    assert f"ExecStart=/usr/bin/python3 {runtime}/napcat-watchdog.py" in napcat
    assert f"{runtime}/fingerprint-namespace.sh" in dropin
    # Non-default paths are injected as path-only Environment= lines:
    # credentials into the companion unit, fingerprint into the drop-in
    # (the fragment that launches fingerprint-namespace.sh).
    assert f"Environment=QQBOT_CREDENTIALS_FILE={credentials}" in companion
    assert f"Environment=QQBOT_FINGERPRINT_DIR={fingerprint}" in dropin
    assert "QQBOT_FINGERPRINT_DIR" not in napcat
    assert "QQBOT_CREDENTIALS_FILE" not in dropin


def test_sync_apply_default_paths_render_no_env_lines(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    result = run_sync({"QQBOT_RUNTIME": str(runtime)}, apply=True)
    assert result.returncode == 0, result.stderr
    for name in ("qqbot-companion.service", "qqbot-napcat.service", "fingerprint.conf"):
        text = (runtime / name).read_text()
        assert "Environment=QQBOT_" not in text
        assert "@BASE@" not in text and "@RUNTIME@" not in text
    assert f"{runtime}/companion-launch.py" in (runtime / "qqbot-companion.service").read_text()


@pytest.mark.parametrize(
    "runtime_subpath",
    ["runtime-24h/fingerprint", "runtime-24h/fingerprint/nested", ""],
)
def test_sync_rejects_runtime_overlapping_fingerprint_dir(
    tmp_path: Path, runtime_subpath: str
) -> None:
    base = tmp_path / "base"
    runtime = base / runtime_subpath if runtime_subpath else base
    result = run_sync({"QQBOT_BASE": str(base), "QQBOT_RUNTIME": str(runtime)})
    assert result.returncode != 0
    assert "fingerprint" in result.stderr


def test_sync_dry_run_skips_symlink_target_without_leaking(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    secret = tmp_path / "secret-credentials.json"
    secret.write_text("TOPSECRET-MARKER")
    secret.chmod(0o600)
    target = runtime / "qqbot-companion.service"
    target.symlink_to(secret)
    result = run_sync({"QQBOT_RUNTIME": str(runtime)})  # dry run
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "SKIP (symlink)" in combined
    # A diff against the link target would have printed its contents.
    assert "TOPSECRET-MARKER" not in combined
    assert target.is_symlink()
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert secret.read_text() == "TOPSECRET-MARKER"


# --- restore-service.sh functional (--check / stop gate, tmp trees) -------------
#
# These run restore-service.sh only against fully isolated fake trees
# (fake base/runtime/fingerprint inputs, isolated systemd dir, and a
# PATH-stubbed systemctl where one is needed). They never touch the
# live units or the out-of-repo runtime.


def _write_fingerprint_inputs(fp_dir: Path) -> None:
    fp_dir.mkdir(parents=True, exist_ok=True)
    fp_dir.chmod(0o700)
    (fp_dir / "machine-id").write_text("a" * 32)
    (fp_dir / "hostname").write_text("testhost")
    (fp_dir / "hostname-file").write_text("testhost")
    mac = codecs.encode("AA-BB-CC-DD-EE-FF", "rot_13").encode()
    (fp_dir / "machine-info").write_bytes(len(mac).to_bytes(4, "big") + mac)
    for entry in fp_dir.iterdir():
        entry.chmod(0o600)


def _build_fake_base(root: Path) -> tuple[Path, Path]:
    base = root / "base"
    runtime = root / "runtime"
    venv_bin = base / "repo" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_python = venv_bin / "python"
    fake_python.write_text("#!/bin/sh\n")
    fake_python.chmod(0o755)
    (base / ".onebot_secret").write_text("x")
    (base / "napcat-v4").mkdir(parents=True)
    (base / "napcat-v4" / "account").write_text("12345")
    runtime.mkdir(parents=True)
    for helper in ("companion-launch.py", "napcat-watchdog.py", "fingerprint-namespace.sh"):
        dest = runtime / helper
        shutil.copyfile(DEPLOY / "runtime" / helper, dest)
        dest.chmod(0o700)
    return base, runtime


def _restore_env(root: Path, base: Path, runtime: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "QQBOT_BASE": str(base),
        "QQBOT_RUNTIME": str(runtime),
        "QQBOT_SYSTEMD_DIR": str(root / "systemd-out"),
        "QQBOT_CREDENTIALS_FILE": str(root / "creds.json"),
    }
    env.update(extra)
    return env


def _require_restore_harness() -> None:
    if os.geteuid() != 0:
        pytest.skip("restore-service.sh requires root")
    probe = subprocess.run(
        ["unshare", "--mount", "--uts", "--", "true"], capture_output=True
    )
    if probe.returncode != 0:
        pytest.skip("unshare --mount --uts not available in this environment")


def _build_runtime_mode_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    _require_restore_harness()
    base, runtime = _build_fake_base(tmp_path)
    (tmp_path / "creds.json").write_text("{}")
    script = runtime / "restore-service.sh"
    shutil.copyfile(RESTORE, script)
    script.chmod(0o700)
    (runtime / "qqbot-companion.service").write_text(
        render_with(COMPANION_TMPL, str(base), str(runtime))
    )
    (runtime / "qqbot-napcat.service").write_text(
        render_with(NAPCAT_TMPL, str(base), str(runtime))
    )
    (runtime / "fingerprint.conf").write_text(
        render_with(FINGERPRINT_TMPL, str(base), str(runtime))
    )
    _write_fingerprint_inputs(runtime / "fingerprint")
    return base, runtime, script


def test_restore_check_accepts_expected_dropin(tmp_path: Path) -> None:
    base, runtime, script = _build_runtime_mode_tree(tmp_path)
    result = subprocess.run(
        ["bash", str(script), "--check"],
        capture_output=True,
        text=True,
        env=_restore_env(tmp_path, base, runtime),
    )
    assert result.returncode == 0, result.stderr
    assert "no changes applied" in result.stdout


def test_restore_check_rejects_wrong_dropin(tmp_path: Path) -> None:
    base, runtime, script = _build_runtime_mode_tree(tmp_path)
    # A tampered drop-in would decide which script root executes; the
    # strict byte-for-byte assertion must fail --check before anything
    # is written.
    (runtime / "fingerprint.conf").write_text(
        "[Service]\nExecStart=\nExecStart=/bin/evil\n"
    )
    result = subprocess.run(
        ["bash", str(script), "--check"],
        capture_output=True,
        text=True,
        env=_restore_env(tmp_path, base, runtime),
    )
    assert result.returncode != 0
    assert "does not match the expected fingerprint drop-in" in result.stderr


def test_restore_check_template_mode_nondefault_env_paths(tmp_path: Path) -> None:
    _require_restore_harness()
    base, runtime = _build_fake_base(tmp_path)
    alt_credentials = tmp_path / "alt-creds.json"
    alt_credentials.write_text("{}")
    alt_fingerprint = tmp_path / "alt-fp"
    _write_fingerprint_inputs(alt_fingerprint)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "restore-service.sh"
    shutil.copyfile(RESTORE, script)
    script.chmod(0o700)
    shutil.copytree(DEPLOY / "systemd", tmp_path / "systemd")
    result = subprocess.run(
        ["bash", str(script), "--check"],
        capture_output=True,
        text=True,
        env=_restore_env(
            tmp_path,
            base,
            runtime,
            QQBOT_CREDENTIALS_FILE=str(alt_credentials),
            QQBOT_FINGERPRINT_DIR=str(alt_fingerprint),
        ),
    )
    # The in-script assertion only passes when the rendered units carry
    # exactly these non-default paths as Environment= lines.
    assert result.returncode == 0, result.stderr
    assert "no changes applied" in result.stdout


def _systemctl_stub(tmp_path: Path) -> Path:
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    stub = stub_dir / "systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == show ]]; then\n'
        '  if [[ "${STUB_STATE:-}" == "__FAIL__" ]]; then exit 1; fi\n'
        '  printf \'%s\\n\' "${STUB_STATE:-inactive}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "stub systemctl: unexpected $*" >&2; exit 1\n'
    )
    stub.chmod(0o755)
    return stub_dir


@pytest.mark.parametrize("stub_state", ["deactivating", "activating", "active"])
def test_restore_stop_gate_refuses_non_settled_states(
    tmp_path: Path, stub_state: str
) -> None:
    base, runtime, script = _build_runtime_mode_tree(tmp_path)
    stub_dir = _systemctl_stub(tmp_path)
    env = _restore_env(tmp_path, base, runtime)
    env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
    env["STUB_STATE"] = stub_state
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert f"ActiveState={stub_state}" in result.stderr


def test_restore_stop_gate_refuses_failed_state_query(tmp_path: Path) -> None:
    base, runtime, script = _build_runtime_mode_tree(tmp_path)
    stub_dir = _systemctl_stub(tmp_path)
    env = _restore_env(tmp_path, base, runtime)
    env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
    env["STUB_STATE"] = "__FAIL__"
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "could not be determined; refusing restore." in result.stderr


# --- repository hygiene ---------------------------------------------------------


def test_no_fingerprint_data_files_in_deploy_tree() -> None:
    names = {p.name for p in DEPLOY.rglob("*") if p.is_file()}
    assert not ({"machine-id", "machine-info", "hostname", "hostname-file"} & names)


def test_deploy_readme_points_at_runbook() -> None:
    text = (DEPLOY / "README.md").read_text()
    assert "docs/deployment-runbook.md" in text


# --- Dockerfile -------------------------------------------------------------------


def test_dockerfile_healthcheck_and_non_root_user() -> None:
    text = (REPO / "Dockerfile").read_text()
    assert "HEALTHCHECK" in text
    assert "127.0.0.1:8090/health/ready" in text
    user_lines = [line.strip() for line in text.splitlines() if line.startswith("USER ")]
    assert user_lines, "Dockerfile must set a USER"
    assert user_lines[-1] == "USER appuser"
    assert "USER root" not in text
    assert "appuser" in text  # created before use
    # Original build steps / entrypoint semantics are preserved.
    assert "uv sync --locked --no-dev --no-install-project" in text
    assert 'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]' in text
    # uv sync installs into /app/.venv; the CMD only resolves if the venv
    # is on PATH.
    assert 'ENV PATH="/app/.venv/bin:${PATH}"' in text
