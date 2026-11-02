#!/usr/bin/env bash
# Read-only health check for the QQBot services.
# This script NEVER starts, stops, restarts or otherwise mutates any
# service; it only observes. Exit code is non-zero when any check FAILs.
#
# Overrides (for rehearsal / relocation):
#   QQBOT_HOST             default 127.0.0.1
#   QQBOT_ONEBOT_PORT      default 3000
#   QQBOT_NAPCAT_PORT      default 6099
#   QQBOT_COMPANION_PORT   default 8090
#   QQBOT_COMPANION_UNIT   default qqbot-companion.service
#   QQBOT_NAPCAT_UNIT      default qqbot-napcat.service
set -u
host="${QQBOT_HOST:-127.0.0.1}"
onebot_port="${QQBOT_ONEBOT_PORT:-3000}"
napcat_port="${QQBOT_NAPCAT_PORT:-6099}"
companion_port="${QQBOT_COMPANION_PORT:-8090}"
companion_unit="${QQBOT_COMPANION_UNIT:-qqbot-companion.service}"
napcat_unit="${QQBOT_NAPCAT_UNIT:-qqbot-napcat.service}"
failures=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; failures=$((failures + 1)); }

check_unit() {
  local unit="$1"
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    pass "unit $unit is active"
  else
    fail "unit $unit is not active"
  fi

}

check_port() {
  local port="$1"
  if python3 - "$host" "$port" <<'PY'
import socket, sys
with socket.socket() as s:
    s.settimeout(3)
    raise SystemExit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PY
  then
    pass "port $port on $host is listening"
  else
    fail "port $port on $host is not listening"
  fi
}

# http_get <url>: print body on stdout; non-zero exit when unreachable.
# No credentials, no redirect following, 3s timeout, proxy bypassed.
http_get() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    # -q must be the first argument: it disables ~/.curlrc so a stray
    # user config (output=, location, ...) cannot make this read-only
    # check write files or follow redirects.
    curl -q -sS --max-time 3 --noproxy '*' "$url"
  else
    python3 - "$url" <<'PY'
import sys, urllib.request
req = urllib.request.Request(sys.argv[1])
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
with opener.open(req, timeout=3) as resp:
    sys.stdout.write(resp.read().decode("utf-8", "replace"))
PY
  fi
}

check_unit "$companion_unit"
check_unit "$napcat_unit"
check_port "$onebot_port"
check_port "$napcat_port"
check_port "$companion_port"

if body="$(http_get "http://$host:$companion_port/health/ready" 2>/dev/null)" \
   && printf '%s' "$body" | grep -Eq '"ready"[[:space:]]*:[[:space:]]*true'; then
  pass "GET /health/ready reports ready=true"
else
  fail "GET /health/ready does not report ready=true"
fi

# OneBot: reachability only. The online/good fields are parsed on a
# best-effort basis and reported, but no conclusion is drawn from them;
# an unreachable endpoint is reported as unreachable, nothing more.
if body="$(http_get "http://$host:$onebot_port/get_status" 2>/dev/null)"; then
  detail=""
  if printf '%s' "$body" | grep -q '"online"'; then detail="$detail online-field"; fi
  if printf '%s' "$body" | grep -q '"good"'; then detail="$detail good-field"; fi
  pass "OneBot /get_status is reachable${detail:+ (found:$detail)}"
else
  fail "OneBot /get_status is unreachable"
fi

if [[ "$failures" -gt 0 ]]; then
  echo "RESULT: FAIL ($failures check(s) failed)"
  exit 1
fi
echo "RESULT: PASS (all checks passed)"
exit 0
