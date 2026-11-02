#!/usr/bin/env bash
# Sync the in-repo deploy assets into the out-of-repo runtime directory.
#
# Default is a dry run: it only prints what would be written, plus a diff
# summary for files that differ. Pass --apply to actually write.
#
# Safety rules:
#   - The fingerprint/ directory is never written.
#   - No *credentials* file and no *.log file is ever written (the sync
#     list below is a fixed whitelist, and is_excluded() double-checks).
#   - A target that is a symlink is always skipped before any
#     cmp/diff/chmod/backup/write step (a diff would otherwise print
#     the link target's contents), and the realpath-resolved target is
#     re-checked against is_excluded() and the fingerprint tree.
#   - The runtime path is normalised with realpath -m up front; a
#     runtime that is, or lives inside, the fingerprint directory --
#     or that (other than the default runtime itself, which
#     legitimately contains fingerprint/ and is guarded by the rules
#     above) contains the fingerprint directory -- is refused.
#   - When --apply would overwrite an existing file whose content
#     differs, the old file is first backed up next to it as
#     <name>.pre-deploy-sync-<UTC timestamp>.
#
# Overrides:
#   QQBOT_BASE     default $HOME/agent-workspace/projects/QQBot-deploy
#   QQBOT_RUNTIME  default $QQBOT_BASE/runtime-24h
#   QQBOT_FINGERPRINT_DIR  default $QQBOT_RUNTIME/fingerprint
#   QQBOT_CREDENTIALS_FILE default $HOME/.local/share/qqbot-service/companion-credentials.json
set -euo pipefail
umask 077
base="${QQBOT_BASE:-$HOME/agent-workspace/projects/QQBot-deploy}"
runtime="${QQBOT_RUNTIME:-$base/runtime-24h}"
# Normalise the runtime path before anything else: symlink tricks or
# ".." segments must not smuggle writes into (or out of) the
# fingerprint tree.
runtime="$(realpath -m "$runtime")"
default_runtime="$(realpath -m "$base/runtime-24h")"
fingerprint_dir_canonical="$(realpath -m "$base/runtime-24h/fingerprint")"
if [[ "$runtime" == "$fingerprint_dir_canonical" || "$runtime" == "$fingerprint_dir_canonical"/* ]]; then
  echo "Refusing: runtime $runtime is the fingerprint directory or inside it." >&2
  exit 1
fi
if [[ "$fingerprint_dir_canonical" == "$runtime"/* && "$runtime" != "$default_runtime" ]]; then
  echo "Refusing: runtime $runtime contains the fingerprint directory $fingerprint_dir_canonical." >&2
  exit 1
fi
default_fingerprint="$runtime/fingerprint"
fingerprint="${QQBOT_FINGERPRINT_DIR:-$default_fingerprint}"
default_credentials="$HOME/.local/share/qqbot-service/companion-credentials.json"
credentials="${QQBOT_CREDENTIALS_FILE:-$default_credentials}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd "$script_dir/.." && pwd)"
apply=0
case "${1:-}" in
  "") ;;
  --apply) apply=1 ;;
  -h|--help) echo 'Usage: sync-to-runtime.sh [--apply]'; exit 0 ;;
  *) echo 'Usage: sync-to-runtime.sh [--apply]' >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo 'Usage: sync-to-runtime.sh [--apply]' >&2; exit 2; }

is_excluded() {
  case "$1" in
    fingerprint|fingerprint/*|*/fingerprint|*/fingerprint/*) return 0 ;;
    *credentials*) return 0 ;;
    *.log) return 0 ;;
    *) return 1 ;;
  esac
}

# PID suffix keeps backup names unique even for repeated --apply runs
# within the same second.
timestamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# Render the templated units once, exactly as restore-service.sh does:
# @BASE@ -> $base, @RUNTIME@ -> the actual (normalised) $runtime, and
# -- only when the resolved path differs from its default -- a
# path-only Environment= line for a non-default credentials file
# (companion unit) or fingerprint dir (fingerprint drop-in).
sed_escape() { printf '%s' "$1" | sed 's/[|&\\]/\\&/g'; }
render() {
  sed -e "s|@BASE@|$(sed_escape "$base")|g" \
      -e "s|@RUNTIME@|$(sed_escape "$runtime")|g" "$1" > "$2"
}
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
render "$deploy_dir/systemd/qqbot-companion.service.tmpl" "$tmpdir/qqbot-companion.service"
apply_env_override "$tmpdir/qqbot-companion.service" QQBOT_CREDENTIALS_FILE "$credentials" "$default_credentials"
render "$deploy_dir/systemd/qqbot-napcat.service.tmpl" "$tmpdir/qqbot-napcat.service"
render "$deploy_dir/systemd/qqbot-napcat.service.d/fingerprint.conf.tmpl" "$tmpdir/fingerprint.conf"
apply_env_override "$tmpdir/fingerprint.conf" QQBOT_FINGERPRINT_DIR "$fingerprint" "$default_fingerprint"

# Fixed whitelist: <source> <target-name-in-runtime> <install-mode>
sync_one() {
  local src="$1" name="$2" mode="$3"
  local target="$runtime/$name"
  # Symlink check first, before cmp/diff/chmod/backup/write: following
  # a link could print or overwrite whatever it points at (e.g. a
  # credentials file). Skip and warn instead.
  if [[ -L "$target" ]]; then
    echo "SKIP (symlink): $target"
    return 0
  fi
  if is_excluded "$name"; then
    echo "SKIP (excluded): $target"
    return 0
  fi
  # Re-check the fully resolved target: a symlinked runtime component
  # could land the real path inside the fingerprint tree or on a
  # credentials / log file even though the whitelist name is clean.
  local resolved
  resolved="$(realpath -m "$target")"
  if is_excluded "$resolved" \
     || [[ "$resolved" == "$fingerprint_dir_canonical" || "$resolved" == "$fingerprint_dir_canonical"/* ]]; then
    echo "SKIP (excluded resolved path): $target -> $resolved"
    return 0
  fi
  if [[ -f "$target" ]] && cmp -s "$src" "$target"; then
    # Content matches; a drifted mode is still drift (e.g. a 0700 root
    # script left at 0644), so compare and repair the mode too.
    local current_mode
    current_mode="$(stat -c '%a' "$target")"
    # stat prints e.g. 700 where the whitelist says 0700; compare as octal.
    if (( 8#$current_mode == 8#$mode )); then
      echo "UNCHANGED: $target"
    elif [[ "$apply" -eq 0 ]]; then
      echo "WOULD FIX MODE: $target ($current_mode -> $mode)"
    else
      chmod "$mode" "$target"
      echo "MODE FIXED: $target ($current_mode -> $mode)"
    fi
    return 0
  fi
  if [[ "$apply" -eq 0 ]]; then
    if [[ -f "$target" ]]; then
      echo "WOULD UPDATE: $target (mode $mode)"
      diff -u "$target" "$src" | head -n 60 || true
    else
      echo "WOULD CREATE: $target (mode $mode)"
    fi
    return 0
  fi
  install -d -m 0700 "$runtime"
  if [[ -f "$target" ]]; then
    backup="$target.pre-deploy-sync-$timestamp"
    cp -a "$target" "$backup"
    echo "BACKUP: $target -> $backup"
  fi
  install -m "$mode" "$src" "$target"
  echo "WROTE: $target (mode $mode)"
}

for name in restore-service.sh companion-launch.py napcat-watchdog.py fingerprint-namespace.sh; do
  sync_one "$deploy_dir/runtime/$name" "$name" 0700
done
sync_one "$tmpdir/qqbot-companion.service" qqbot-companion.service 0644
sync_one "$tmpdir/qqbot-napcat.service" qqbot-napcat.service 0644
sync_one "$tmpdir/fingerprint.conf" fingerprint.conf 0644

if [[ "$apply" -eq 0 ]]; then
  echo "Dry run only; re-run with --apply to write."
fi
