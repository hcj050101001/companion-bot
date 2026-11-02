#!/usr/bin/env python3
import json
import os
from pathlib import Path


def _default_base() -> Path:
    # When this script runs from its installed home ($BASE/runtime-24h),
    # derive the base from its own location so a relocated tree works
    # without any environment wiring; QQBOT_BASE still wins when set.
    here = Path(__file__).resolve().parent
    if here.name == "runtime-24h":
        return here.parent
    return Path.home() / "agent-workspace" / "projects" / "QQBot-deploy"


base = Path(os.environ.get("QQBOT_BASE", _default_base()))
credentials_file = Path(
    os.environ.get(
        "QQBOT_CREDENTIALS_FILE",
        str(Path.home() / ".local" / "share" / "qqbot-service" / "companion-credentials.json"),
    )
)
with open(credentials_file) as source:
    saved = json.load(source)
secret = (base / ".onebot_secret").read_text().strip()
if not saved.get("DEEPSEEK_API_KEY") or not secret:
    raise SystemExit("Required companion credentials are missing")
for name in ("NO_PROXY", "no_proxy"):
    hosts = ("localhost", "127.0.0.1", "::1")
    if not all(host in saved.get(name, "").split(",") for host in hosts):
        raise SystemExit("Local proxy bypass is incomplete")
env = os.environ.copy()
env.update(saved)
env["ONEBOT_SECRET"] = secret
os.chdir(base / "repo")
python = str(base / "repo/.venv/bin/python")
args = [
    python, "-m", "uvicorn", "main:app",
    "--host", "127.0.0.1", "--port", "8090", "--no-access-log",
]
os.execve(python, args, env)
