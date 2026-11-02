#!/usr/bin/env bash
set -euo pipefail
umask 077
# Base derivation: QQBOT_BASE wins; otherwise, when this script runs
# from its installed home ($BASE/runtime-24h), derive the base from its
# own location so a relocated tree works without environment wiring.
if [[ -n "${QQBOT_BASE:-}" ]]; then
  base="$QQBOT_BASE"
else
  self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ "$(basename "$self_dir")" == runtime-24h ]]; then
    base="$(cd "$self_dir/.." && pwd)"
  else
    base="$HOME/agent-workspace/projects/QQBot-deploy"
  fi
fi
runtime="${QQBOT_RUNTIME:-$base/runtime-24h}"
# QQBOT_FINGERPRINT_DIR is honored here as well as in restore-service.sh
# so validation and the running service read one and the same tree.
fingerprint="${QQBOT_FINGERPRINT_DIR:-$runtime/fingerprint}"
# This runs only inside the private mount and UTS namespaces created by systemd.
mount --make-rprivate /
mount --bind "$fingerprint/machine-id" /etc/machine-id
if [[ -e /var/lib/dbus/machine-id && ! -L /var/lib/dbus/machine-id ]]; then
  mount --bind "$fingerprint/machine-id" /var/lib/dbus/machine-id
fi
mount --bind "$fingerprint/hostname-file" /etc/hostname
hostname -F "$fingerprint/hostname"
exec /usr/bin/python3 "$runtime/napcat-watchdog.py"
