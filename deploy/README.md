# deploy/ — QQBot deployment assets

Version-controlled copies of the deployment assets that used to live only
out-of-repo in `runtime-24h/` next to the checkout. The full operational
procedure (restore, rehearsal, rollback) is in
[docs/deployment-runbook.md](../docs/deployment-runbook.md); this file only
describes the layout of this directory.

## Layout

```
deploy/
  systemd/
    qqbot-companion.service.tmpl                  # rendered -> qqbot-companion.service
    qqbot-napcat.service.tmpl                     # rendered -> qqbot-napcat.service
    qqbot-napcat.service.d/fingerprint.conf.tmpl  # rendered -> fingerprint.conf drop-in
  runtime/
    restore-service.sh        # root-only restore / --check validation
    companion-launch.py       # injects credentials, execs uvicorn on 127.0.0.1:8090
    napcat-watchdog.py        # (re)starts NapCat when QQ is down and ports are free
    fingerprint-namespace.sh  # bind-mounts fingerprint inputs inside the namespace
  bin/
    health-check.sh           # read-only PASS/FAIL probe of units, ports, HTTP
    sync-to-runtime.sh        # dry-run by default; --apply syncs into runtime-24h/
```

## Templates

The systemd templates use two placeholders: `@BASE@` for the deploy
base path (production value:
`<deploy-root>`) and `@RUNTIME@`
for the runtime directory (default rendering value:
`@BASE@/runtime-24h`; restore-service.sh and sync-to-runtime.sh render
it with the actual `QQBOT_RUNTIME`). Rendering is a plain substitution:

```sh
sed -e "s|@BASE@|$QQBOT_BASE|g" -e "s|@RUNTIME@|${QQBOT_RUNTIME:-$QQBOT_BASE/runtime-24h}|g" \
  deploy/systemd/qqbot-companion.service.tmpl
```

When the resolved credentials file or fingerprint directory differs
from its default, rendering additionally injects a path-only
`Environment=QQBOT_CREDENTIALS_FILE=` line into the companion unit /
`Environment=QQBOT_FINGERPRINT_DIR=` line into the fingerprint drop-in;
with default paths the rendered units carry no such lines.

Apart from the placeholder, the rendered units are line-for-line identical
to the units currently installed under `/etc/systemd/system`.

## Path overrides

All scripts honour environment overrides so the same assets can be
pointed at a relocated tree (e.g. after a machine move). Note that
`QQBOT_SYSTEMD_DIR` is **not** an isolation switch: restore-service.sh
activates units via systemctl only when it points at the real
`/etc/systemd/system`, and merely installs files (printing
"NOT activated") otherwise — real rehearsal belongs in a separate
VM/container. Also note `sudo` drops environment variables; pass
overrides as `sudo QQBOT_BASE=... bash ...`.

| Variable | Default |
| --- | --- |
| `QQBOT_BASE` | `$HOME/agent-workspace/projects/QQBot-deploy` |
| `QQBOT_RUNTIME` | `$QQBOT_BASE/runtime-24h` |
| `QQBOT_SYSTEMD_DIR` | `/etc/systemd/system` (restore-service.sh only) |
| `QQBOT_FINGERPRINT_DIR` | `$QQBOT_RUNTIME/fingerprint` |
| `QQBOT_CREDENTIALS_FILE` | `$HOME/.local/share/qqbot-service/companion-credentials.json` |

The runtime helpers (`companion-launch.py`, `napcat-watchdog.py`,
`fingerprint-namespace.sh`) additionally derive their base from their
own location when they run from a directory named `runtime-24h`, so a
relocated tree works even when no variable reaches the service
environment; `QQBOT_BASE` still wins when set.

`health-check.sh` additionally honours `QQBOT_HOST`, `QQBOT_ONEBOT_PORT`
(3000), `QQBOT_NAPCAT_PORT` (6099) and `QQBOT_COMPANION_PORT` (8090).

## Intentional differences from the out-of-repo restore script

The main intentional differences (safety semantics are unchanged —
every check of the out-of-repo version is preserved: root required,
refusal while a managed service is active, while QQ is running, or
while ports 3000/6099/8090 are occupied, fingerprint input validation,
`--check` mode, machine-info-only install, `enable --now`):

1. `restore-service.sh` installs the companion unit as a rendered
   regular file (`install -m 0644`) instead of symlinking
   `/etc/systemd/system/qqbot-companion.service` into `runtime-24h/`.
   A regular file is auditable on its own and cannot silently change
   when the runtime directory is edited.
2. Paths are overridable via the `QQBOT_*` variables above, with the
   activation guard for non-default `QQBOT_SYSTEMD_DIR` described
   there.
3. Unit sources are single-mode: either all three sibling templates,
   or — when no sibling template tree exists, e.g. the script was
   synced into `runtime-24h/` — the already-rendered unit files in
   `$QQBOT_RUNTIME`, which are accepted only after asserting they
   reference the current `QQBOT_BASE`. Partial template trees are
   refused instead of mixed.
4. Runtime helpers missing from `$QQBOT_RUNTIME` are installed from
   the script's own directory before units go in, and the fingerprint
   validation runs with `PYTHONOPTIMIZE` stripped so its asserts can
   never be optimised away.

## Docker path (not the production path)

Production runs under systemd (above). The repo `Dockerfile` /
`docker-compose.yml` path is **unverified** (no Docker on the
operations host) and is not a substitute. If it is ever used: the
image runs as uid 10001 (`appuser`); compose bind-mounts
`./config.yaml` and `./config` read-only, so they must be readable by
uid 10001 on the host, and `./data` / `./logs` must be writable by
uid 10001 (e.g. `chown -R 10001:10001 data logs`). Production
`config.yaml` / `config/` are excluded from the build context via
`.dockerignore` and are only ever supplied by the bind mounts.

## Safety discipline

- `health-check.sh` is strictly read-only: it never starts, stops or
  restarts anything.
- `sync-to-runtime.sh` is a dry run unless `--apply` is given, backs up
  differing targets as `<name>.pre-deploy-sync-<UTC timestamp>`, and
  never writes `fingerprint/`, any `*credentials*` file, or any `*.log`.
- No secret or fingerprint *values* live in this repository — only the
  validation logic and scripts. The actual `fingerprint/` data files
  (machine-id, hostname, machine-info, …) stay out-of-repo with mode
  0600 in a 0700 directory.
