#!/usr/bin/env bash
set -euo pipefail
umask 077
# Deploy-assets copy of the out-of-repo runtime-24h/restore-service.sh.
#
# Paths are overridable for rehearsal / relocation:
#   QQBOT_BASE            default $HOME/agent-workspace/projects/QQBot-deploy
#   QQBOT_RUNTIME         default $QQBOT_BASE/runtime-24h
#   QQBOT_SYSTEMD_DIR     default /etc/systemd/system (point at an isolated
#                         root to rehearse without touching the live units)
#   QQBOT_FINGERPRINT_DIR default $QQBOT_RUNTIME/fingerprint
#   QQBOT_CREDENTIALS_FILE default $HOME/.local/share/qqbot-service/companion-credentials.json
#
# Unit sources are single-mode, never mixed per file:
#   - template mode: all three sibling templates exist
#     (../systemd/*.tmpl relative to this script, i.e. deploy/systemd/
#     when this script lives in deploy/runtime/) and units are rendered
#     from them with sed s|@BASE@|$base|g and
#     s|@RUNTIME@|$runtime|g (@RUNTIME@ defaults to $base/runtime-24h,
#     so the default rendering is unchanged);
#   - runtime mode: no sibling template exists (e.g. this script has
#     been synced into runtime-24h/) and the already-rendered unit files
#     $QQBOT_RUNTIME/qqbot-napcat.service / qqbot-companion.service are
#     used after asserting they reference $QQBOT_BASE and the actual
#     $QQBOT_RUNTIME. The fingerprint drop-in is NEVER copied from
#     $QQBOT_RUNTIME/fingerprint.conf: that file decides which script
#     root executes, so when present it is first asserted byte-for-byte
#     against the expected drop-in (in every mode that would consume
#     it, including --check, before anything is written), and the
#     installed drop-in is always generated inline from the effective
#     paths.
# A partial template tree is an error, not a fallback trigger.
#
# Environment wiring: only when the resolved credentials / fingerprint
# paths differ from their defaults does rendering inject a path-only
# Environment= line -- QQBOT_CREDENTIALS_FILE into the companion unit,
# QQBOT_FINGERPRINT_DIR into the fingerprint drop-in (the fragment
# whose ExecStart launches fingerprint-namespace.sh, the reader of
# that variable). No credential or fingerprint VALUES are ever
# rendered, only paths. --check additionally asserts that the rendered
# units reference exactly the base / runtime / credentials /
# fingerprint paths validated in this run.
#
# Activation guard: systemctl daemon-reload / enable --now run ONLY
# when QQBOT_SYSTEMD_DIR is the real /etc/systemd/system. Pointing the
# script at an isolated directory installs files there and stops --
# systemd would otherwise activate the LIVE same-named units.
#
# Intentional differences from the out-of-repo version (safety
# semantics unchanged): the companion unit is installed as a rendered
# regular file via `install -m 0644` instead of a symlink into
# runtime-24h/ (auditable on its own); paths are overridable via the
# QQBOT_* variables above; unit sources follow the single-mode rule
# above; runtime helper scripts missing from $QQBOT_RUNTIME are
# installed from this script's own directory before units go in.
base="${QQBOT_BASE:-$HOME/agent-workspace/projects/QQBot-deploy}"
runtime="${QQBOT_RUNTIME:-$base/runtime-24h}"
systemd_dir="${QQBOT_SYSTEMD_DIR:-/etc/systemd/system}"
default_fingerprint="$runtime/fingerprint"
fingerprint="${QQBOT_FINGERPRINT_DIR:-$default_fingerprint}"
default_credentials="$HOME/.local/share/qqbot-service/companion-credentials.json"
credentials="${QQBOT_CREDENTIALS_FILE:-$default_credentials}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template_dir="$script_dir/../systemd"
mode=${1:-restore}
[[ "$mode" == restore || "$mode" == --check ]] && [[ $# -le 1 ]] || { echo 'Usage: restore-service.sh [--check]' >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo 'Run as root.' >&2; exit 1; }
# Echo the effective paths (never credential contents) so an operator
# can confirm overrides before anything is touched.
echo "restore-service.sh: base=$base runtime=$runtime systemd_dir=$systemd_dir fingerprint=$fingerprint credentials=$credentials"
test -s "$credentials"
test -s "$base/.onebot_secret"
test -x "$base/repo/.venv/bin/python"
# The units execute $runtime/{companion-launch.py,napcat-watchdog.py}
# (the latter via fingerprint-namespace.sh). Each helper must exist in
# $runtime or next to this script (restore installs missing ones from
# the script directory before any unit goes in).
for helper in companion-launch.py napcat-watchdog.py fingerprint-namespace.sh; do
  if [[ ! -x "$runtime/$helper" && ! -x "$script_dir/$helper" ]]; then
    echo "$helper is missing or not executable in both $runtime and $script_dir." >&2; exit 1
  fi
done
# Unit source mode: template (all three templates) or runtime (none).
template_count=0
for t in qqbot-napcat.service qqbot-companion.service qqbot-napcat.service.d/fingerprint.conf; do
  [[ -r "$template_dir/$t.tmpl" ]] && template_count=$((template_count + 1))
done
if [[ "$template_count" -eq 3 ]]; then
  unit_mode=template
elif [[ "$template_count" -eq 0 ]]; then
  unit_mode=runtime
  for u in qqbot-napcat.service qqbot-companion.service; do
    [[ -r "$runtime/$u" ]] || { echo "$u source is missing in $runtime." >&2; exit 1; }
    # A pre-rendered unit from another base must never be installed.
    grep -qF "WorkingDirectory=$base" "$runtime/$u" || { echo "$u does not reference base $base; refusing." >&2; exit 1; }
    grep -qF "$runtime/" "$runtime/$u" || { echo "$u does not reference runtime $runtime/; refusing." >&2; exit 1; }
  done
else
  echo "Partial template tree in $template_dir ($template_count/3); refusing mixed unit sources." >&2; exit 1
fi
sed_escape() { printf '%s' "$1" | sed 's/[|&\\]/\\&/g'; }
# apply_env_override <file> <VAR> <resolved> <default>: drop any stale
# Environment=<VAR>= line, then -- only when the resolved path differs
# from the default -- inject Environment=<VAR>=<resolved> (a path,
# never a secret value) right after the [Service] header.
apply_env_override() {
  local file="$1" var="$2" resolved="$3" default="$4"
  local tmp
  tmp="$(mktemp)"
  grep -v "^Environment=${var}=" "$file" > "$tmp" || true
  cat "$tmp" > "$file"
  rm -f "$tmp"
  if [[ "$resolved" != "$default" ]]; then
    tmp="$(mktemp)"
    QQBOT_ENV_LINE="Environment=${var}=${resolved}" awk \
      'BEGIN { line = ENVIRON["QQBOT_ENV_LINE"] }
       { print }
       $0 == "[Service]" { print line }' "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
  fi
}
# print_expected_fingerprint_conf: the one and only drop-in content
# this script will install or accept, built from the effective paths.
print_expected_fingerprint_conf() {
  printf '[Service]\n'
  if [[ "$fingerprint" != "$default_fingerprint" ]]; then
    printf 'Environment=QQBOT_FINGERPRINT_DIR=%s\n' "$fingerprint"
  fi
  printf 'ExecStart=\n'
  printf 'ExecStart=/usr/bin/unshare --mount --uts -- %s/fingerprint-namespace.sh\n' "$runtime"
}
# The runtime-mode drop-in decides which script root executes, so it
# must never be trusted unverified: when $runtime/fingerprint.conf
# exists, assert -- before any write and also in --check mode -- that
# its content is exactly the expected drop-in. Refuse otherwise.
if [[ "$unit_mode" == runtime && -e "$runtime/fingerprint.conf" ]]; then
  if ! cmp -s <(print_expected_fingerprint_conf) "$runtime/fingerprint.conf"; then
    echo "$runtime/fingerprint.conf does not match the expected fingerprint drop-in; refusing." >&2
    exit 1
  fi
fi
# render_unit <name> <output> according to the single source mode.
render_unit() {
  local name="$1" out="$2"
  if [[ "$unit_mode" == template ]]; then
    sed -e "s|@BASE@|$(sed_escape "$base")|g" \
        -e "s|@RUNTIME@|$(sed_escape "$runtime")|g" \
        "$template_dir/$name.tmpl" > "$out"
  else
    cat "$runtime/$name" > "$out"
  fi
  if [[ "$name" == qqbot-companion.service ]]; then
    apply_env_override "$out" QQBOT_CREDENTIALS_FILE "$credentials" "$default_credentials"
  fi
}
# render_fingerprint_conf <output>: template in template mode; in
# runtime mode the expected drop-in is generated inline (never copied
# from $runtime/fingerprint.conf -- see the assertion above).
render_fingerprint_conf() {
  local out="$1"
  if [[ "$unit_mode" == template ]]; then
    sed -e "s|@BASE@|$(sed_escape "$base")|g" \
        -e "s|@RUNTIME@|$(sed_escape "$runtime")|g" \
        "$template_dir/qqbot-napcat.service.d/fingerprint.conf.tmpl" > "$out"
    apply_env_override "$out" QQBOT_FINGERPRINT_DIR "$fingerprint" "$default_fingerprint"
  else
    print_expected_fingerprint_conf > "$out"
  fi
}
# assert_rendered_paths <dir>: the rendered units in <dir> must
# reference exactly this run's base / runtime / credentials /
# fingerprint paths -- no leftover placeholders, no stale paths.
assert_rendered_paths() {
  local dir="$1"
  local companion="$dir/qqbot-companion.service"
  local napcat="$dir/qqbot-napcat.service"
  local dropin="$dir/fingerprint.conf"
  if grep -q '@BASE@\|@RUNTIME@' "$companion" "$napcat" "$dropin"; then
    echo 'Rendered units still contain @BASE@/@RUNTIME@ placeholders; refusing.' >&2; exit 1
  fi
  grep -qF "WorkingDirectory=$base" "$companion" || { echo 'Rendered companion unit does not reference the validated base; refusing.' >&2; exit 1; }
  grep -qF "$runtime/companion-launch.py" "$companion" || { echo 'Rendered companion unit does not reference the validated runtime; refusing.' >&2; exit 1; }
  grep -qF "WorkingDirectory=$base" "$napcat" || { echo 'Rendered napcat unit does not reference the validated base; refusing.' >&2; exit 1; }
  grep -qF "$runtime/napcat-watchdog.py" "$napcat" || { echo 'Rendered napcat unit does not reference the validated runtime; refusing.' >&2; exit 1; }
  grep -qF "$runtime/fingerprint-namespace.sh" "$dropin" || { echo 'Rendered fingerprint drop-in does not reference the validated runtime; refusing.' >&2; exit 1; }
  if [[ "$credentials" != "$default_credentials" ]]; then
    grep -qF "Environment=QQBOT_CREDENTIALS_FILE=$credentials" "$companion" || { echo 'Rendered companion unit does not reference the validated credentials path; refusing.' >&2; exit 1; }
  elif grep -q '^Environment=QQBOT_CREDENTIALS_FILE=' "$companion"; then
    echo 'Rendered companion unit carries a credentials path but the default is in effect; refusing.' >&2; exit 1
  fi
  if [[ "$fingerprint" != "$default_fingerprint" ]]; then
    grep -qF "Environment=QQBOT_FINGERPRINT_DIR=$fingerprint" "$dropin" || { echo 'Rendered fingerprint drop-in does not reference the validated fingerprint path; refusing.' >&2; exit 1; }
  elif grep -q '^Environment=QQBOT_FINGERPRINT_DIR=' "$dropin"; then
    echo 'Rendered fingerprint drop-in carries a fingerprint path but the default is in effect; refusing.' >&2; exit 1
  fi
}
# env -u PYTHONOPTIMIZE: the validation below uses assert; it must
# never be silently skipped by an optimized interpreter.
env -u PYTHONOPTIMIZE python3 - "$fingerprint" "$base/napcat-v4/account" <<'PY'
import codecs, os, re, stat, sys
from pathlib import Path
root=Path(sys.argv[1])
assert root.is_dir() and stat.S_IMODE(root.stat().st_mode) == 0o700
for name in ('machine-id','hostname','hostname-file','machine-info'):
 p=root/name
 assert p.is_file() and not p.is_symlink() and stat.S_IMODE(p.stat().st_mode) == 0o600
machine=(root/'machine-id').read_bytes().strip()
host=(root/'hostname').read_bytes().strip()
data=(root/'machine-info').read_bytes()
assert re.fullmatch(rb'[0-9a-fA-F]{32}', machine)
assert 0 < len(host) <= 253 and b'\n' not in host and b'\x00' not in host
assert len(data) >= 4 and len(data) == 4 + int.from_bytes(data[:4], 'big')
assert re.fullmatch(r'[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5}', codecs.decode(data[4:].decode('ascii'), 'rot_13'))
assert re.fullmatch(rb'[0-9]+', Path(sys.argv[2]).read_bytes().strip())
PY
unshare --mount --uts -- true
# Render all units up front (pure, writes only to a private tmpdir) and
# assert the rendered paths are exactly the paths validated above, so
# --check proves the installable units match this run -- and a bad
# render refuses before anything on disk is touched.
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
render_unit qqbot-napcat.service "$tmpdir/qqbot-napcat.service"
render_unit qqbot-companion.service "$tmpdir/qqbot-companion.service"
render_fingerprint_conf "$tmpdir/fingerprint.conf"
assert_rendered_paths "$tmpdir"
if [[ "$mode" == --check ]]; then
  echo 'Fingerprint inputs, account format, and namespace capability validated; no changes applied.'
  exit 0
fi
# Never replace fingerprint inputs while a managed service or QQ process is running.
# Read ActiveState explicitly: only a settled inactive/failed unit is
# safe. Transitional states (activating/deactivating/...) and a failed
# systemctl query both refuse -- is-active's non-zero exit would have
# let a deactivating unit slip through.
for unit in qqbot-napcat.service qqbot-companion.service; do
  if ! state="$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)"; then
    echo "ActiveState of $unit could not be determined; refusing restore." >&2; exit 1
  fi
  case "$state" in
    inactive|failed) ;;
    *) echo "A managed service is active; refusing restore. ($unit ActiveState=${state:-unknown})" >&2; exit 1 ;;
  esac
done
env -u PYTHONOPTIMIZE python3 - "$base/napcat-v4/root/opt/QQ/qq" <<'PY'
import os, socket, sys
from pathlib import Path
qq=sys.argv[1]
for proc in Path('/proc').iterdir():
 if proc.name.isdigit():
  try:
   if os.readlink(proc/'exe').removesuffix(' (deleted)') == qq:
    raise SystemExit('QQ is running; refusing restore.')
  except (FileNotFoundError, ProcessLookupError, PermissionError):
   pass
for port in (3000, 6099, 8090):
 with socket.socket() as s:
  s.settimeout(0.1)
  if s.connect_ex(('127.0.0.1', port)) == 0:
   raise SystemExit('A service port is occupied; refusing restore.')
PY
# Install any missing runtime helpers from this script's directory so
# the units never point at nonexistent executables.
for helper in companion-launch.py napcat-watchdog.py fingerprint-namespace.sh; do
  if [[ ! -x "$runtime/$helper" ]]; then
    install -m 0700 "$script_dir/$helper" "$runtime/$helper"
  fi
done
# Replace only the device file, never account or session data.
install -d -m 0700 "$base/napcat-v4/home/.config/QQ/global/nt_data/msf"
install -m 0600 "$fingerprint/machine-info" "$base/napcat-v4/home/.config/QQ/global/nt_data/msf/machine-info"
# Units were rendered and path-asserted above; install those files.
install -m 0644 "$tmpdir/qqbot-napcat.service" "$systemd_dir/qqbot-napcat.service"
install -d -m 0755 "$systemd_dir/qqbot-napcat.service.d"
install -m 0600 "$tmpdir/fingerprint.conf" "$systemd_dir/qqbot-napcat.service.d/fingerprint.conf"
# Intentional difference (see header): rendered regular file, not a symlink.
# Remove any previous symlink first so install never writes through it.
rm -f "$systemd_dir/qqbot-companion.service"
install -m 0644 "$tmpdir/qqbot-companion.service" "$systemd_dir/qqbot-companion.service"
# Activation guard (see header): only the real systemd dir is activated.
if [[ "$systemd_dir" == /etc/systemd/system ]]; then
  systemctl daemon-reload
  systemctl enable --now qqbot-napcat.service qqbot-companion.service
else
  echo "Isolated systemd_dir ($systemd_dir): unit files installed, NOT activated. No live unit was touched."
fi
