"""NapCat health monitor with one restart attempt per outage."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable

import httpx


def qq_process_exists() -> bool:
    """Check process names without depending on a platform-specific ps package."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "[Qq][Qq]"], capture_output=True, check=False, timeout=5
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class NapCatWatchdog:
    def __init__(
        self,
        base_url: str,
        alert_path: Path,
        start_cmd: str | None = None,
        *,
        process_check: Callable[[], bool] = qq_process_exists,
        http_get: Callable[..., object] | None = None,
        port_check: Callable[[str, int], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        restart: Callable[[str], bool] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.alert_path = alert_path
        self.start_cmd = start_cmd if start_cmd is not None else os.getenv("NAPCAT_START_CMD", "")
        self.process_check = process_check
        self.http_get = http_get or httpx.get
        self.port_check = port_check or self._port_open
        self.clock = clock
        self.restart = restart or self._restart
        self.down_since: float | None = None
        self.restart_attempted = False
        self.alerted = False

    @staticmethod
    def _port_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            return False

    @staticmethod
    def _restart(command: str) -> bool:
        if not command:
            return False
        try:
            return subprocess.run(command, shell=True, check=False, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _healthy(self) -> bool:
        if not self.process_check():
            return False
        try:
            response = self.http_get(f"{self.base_url}/get_status", timeout=5)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("online") is not True:
                return False
            from urllib.parse import urlparse

            parsed = urlparse(self.base_url)
            return self.port_check(
                parsed.hostname or "127.0.0.1",
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        except (httpx.HTTPError, OSError, ValueError, AttributeError):
            return False

    def _alert(self) -> None:
        self.alert_path.parent.mkdir(parents=True, exist_ok=True)
        with self.alert_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps({"category": "napcat_down", "timestamp": int(time.time())}) + "\n"
            )

    def tick(self) -> bool:
        """Run one health check. Return current health; safe for deterministic tests."""
        if self._healthy():
            self.down_since = None
            self.restart_attempted = False
            self.alerted = False
            return True
        now = self.clock()
        if self.down_since is None:
            self.down_since = now
        if not self.restart_attempted:
            self.restart_attempted = True
            self.restart(self.start_cmd)
        if now - self.down_since >= 300 and not self.alerted:
            self._alert()
            self.alerted = True
        return False

    async def run(self, interval: float = 30) -> None:
        while True:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(interval)


def main() -> None:
    base_url = os.getenv("NAPCAT_BASE_URL", "http://127.0.0.1:3000")
    logs_dir = Path(os.getenv("LOGS_DIR", "logs"))
    asyncio.run(NapCatWatchdog(base_url, logs_dir / "alerts.jsonl").run())


if __name__ == "__main__":
    main()
