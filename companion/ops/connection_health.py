"""Bounded, read-only local observation; never controls processes or sends messages."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import random
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("companion.connection_health")
MAX_BODY = 4096
PROBE_TIMEOUT = 3.0
HEALTHY_INTERVAL = 30.0
CEILING = 300.0
STABLE_SECONDS = 120.0


def local_url(url: str) -> str:
    parsed = urlsplit(url)
    try:
        local = (parsed.hostname == "localhost" or
                 ipaddress.ip_address(parsed.hostname or "").is_loopback)
        port = parsed.port
    except ValueError:
        raise ValueError("health URL must use a loopback address") from None
    if (not local or parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or not port):
        raise ValueError("health URL must be a loopback HTTP origin with a port")
    return url.rstrip("/")


@dataclass(frozen=True)
class UnitState:
    # active refers to the unit's main process, which may itself be a watchdog.
    state: str = "unknown"
    result: str = "unknown"


async def systemd_state(unit: str) -> UnitState:
    """Only query fixed unit names; ignore all other systemd properties."""
    if unit not in {"qqbot-companion.service", "qqbot-napcat.service"}:
        raise ValueError("invalid unit")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl", "show", unit, "--property=ActiveState", "--property=Result",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), PROBE_TIMEOUT)
        if process.returncode != 0 or len(out) > 1024:
            return UnitState()
        values = dict(line.split("=", 1) for line in out.decode().splitlines() if "=" in line)
        active = values.get("ActiveState", "unknown")
        result = values.get("Result", "unknown")
        if active not in {"active", "inactive", "failed", "activating", "deactivating"}:
            active = "unknown"
        if result not in {"success", "start-limit-hit"}:
            result = "other"
        return UnitState(active, result)
    except (OSError, UnicodeError, ValueError, asyncio.TimeoutError):
        return UnitState()
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def get_json(url: str) -> tuple[str, object]:
    """One request, fresh client, no redirects/proxy, bounded time and body."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, trust_env=False) as client:
                async with client.stream("GET", url, follow_redirects=False) as response:
                    if response.status_code != 200:
                        return "http_error", None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            return "invalid", None
                    try:
                        return "ok", json.loads(body)
                    except (ValueError, UnicodeError):
                        return "invalid", None
    except (httpx.HTTPError, OSError, TimeoutError):
        return "disconnected", None


@dataclass(frozen=True)
class Snapshot:
    companion_process: str
    napcat_process: str
    companion_result: str
    napcat_result: str
    ingress: str
    onebot: str
    qq: str
    action: str


class ConnectionHealth:
    def __init__(self, onebot_origin: str, *, ingress_origin: str = "http://127.0.0.1:8090",
                 probe=get_json, unit_state=systemd_state, clock=time.monotonic,
                 jitter=random.uniform, sleep=asyncio.sleep):
        self.onebot_origin = local_url(onebot_origin)
        self.ingress_origin = local_url(ingress_origin)
        self.probe, self.unit_state, self.clock, self.jitter, self.sleep = (
            probe, unit_state, clock, jitter, sleep
        )
        self.failures = 0
        self.stable_since = None
        self.snapshot = None
        self.lock = asyncio.Lock()

    async def tick(self) -> float:
        # A single in-flight cycle prevents concurrent fault triggers from multiplying probes.
        async with self.lock:
            companion, napcat = await asyncio.gather(
                self.unit_state("qqbot-companion.service"), self.unit_state("qqbot-napcat.service")
            )
            ingress_result = await self.probe(self.ingress_origin + "/health/ready")
            ingress = "ready" if ingress_result == ("ok", {"ready": True}) else "unready"
            if napcat.state in {"inactive", "failed", "deactivating"}:
                onebot, qq = "not_probed", "unconfirmed"
            else:
                result, payload = await self.probe(self.onebot_origin + "/get_status")
                onebot = ("reachable" if result in {"ok", "http_error", "invalid"}
                          else "disconnected")
                data = payload.get("data") if isinstance(payload, dict) else None
                if (result == "ok" and isinstance(payload, dict) and payload.get("status") == "ok"
                        and payload.get("retcode") == 0 and isinstance(data, dict)
                        and data.get("online") is True and data.get("good") is True):
                    qq = "reported_online"
                elif (result == "ok" and isinstance(payload, dict)
                      and payload.get("status") == "ok" and payload.get("retcode") == 0
                      and isinstance(data, dict) and data.get("online") is False):
                    qq = "offline"
                else:
                    qq = "unconfirmed"
            healthy = (ingress == "ready" and qq == "reported_online"
                       and companion.state == "active" and napcat.state == "active")
            action = "none" if healthy else "operator_action_needed"
            snapshot = Snapshot(companion.state, napcat.state, companion.result, napcat.result,
                                ingress, onebot, qq, action)
            if snapshot != self.snapshot:
                logger.info(
                    "health companion_process=%s napcat_process=%s companion_result=%s "
                    "napcat_result=%s ingress=%s onebot=%s qq=%s action=%s",
                    companion.state, napcat.state, companion.result, napcat.result,
                    ingress, onebot, qq, action,
                )
            self.snapshot = snapshot
            now = self.clock()
            if healthy:
                if self.stable_since is None:
                    self.stable_since = now
                if now - self.stable_since >= STABLE_SECONDS:
                    self.failures = 0
                return HEALTHY_INTERVAL
            self.stable_since = None
            # A reachable but offline/invalid login requires operator action, not escalation.
            if (onebot == "reachable" and qq != "reported_online") or onebot == "not_probed":
                return CEILING
            self.failures = min(self.failures + 1, 10)
            base = min(CEILING, 5.0 * 2 ** (self.failures - 1))
            return min(CEILING, max(1.0, base * self.jitter(0.8, 1.2)))

    async def run(self) -> None:
        while True:
            await self.sleep(await self.tick())
