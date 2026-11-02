#!/usr/bin/env python3
import fcntl
import os
import socket
import subprocess
import time
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
qq = str(base / "napcat-v4/root/opt/QQ/qq")
lockfile = base / "napcat-v4/service.lock"

def qq_running():
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if os.readlink(proc / "exe") != qq:
                continue
            args = (proc / "cmdline").read_bytes().split(b"\0")
            if not any(arg.startswith(b"--type=") for arg in args):
                return True
        except (OSError, ProcessLookupError):
            continue
    return False

def port_busy(port):
    with socket.socket() as sock:
        sock.settimeout(0.05)
        return sock.connect_ex(("127.0.0.1", port)) == 0

def lock_available():
    with lockfile.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

child = None
while True:
    if child is not None and child.poll() is not None:
        child = None
    ready = (
        child is None
        and not qq_running()
        and not port_busy(3000)
        and not port_busy(6099)
        and lock_available()
    )
    if ready:
        child = subprocess.Popen(
            [str(base / "napcat-control.sh"), "run"], cwd=base, stdin=subprocess.DEVNULL
        )
    time.sleep(0.2)
