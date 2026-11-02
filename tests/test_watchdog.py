import json

from companion.ops.watchdog import NapCatWatchdog


class Response:
    def __init__(self, online):
        self.online = online

    def raise_for_status(self):
        pass

    def json(self):
        return {"online": self.online}


def make_watchdog(tmp_path, monkeypatch, *, healthy=False, restart=lambda _cmd: False):
    monkeypatch.setenv("NAPCAT_START_CMD", "start-napcat")
    state = {"healthy": healthy, "now": 0, "starts": 0}

    def get(*_args, **_kwargs):
        return Response(state["healthy"])

    def start(cmd):
        state["starts"] += 1
        return restart(cmd)

    watchdog = NapCatWatchdog(
        "http://localhost:3000",
        tmp_path / "logs/alerts.jsonl",
        process_check=lambda: True,
        http_get=get,
        port_check=lambda *_: True,
        clock=lambda: state["now"],
        restart=start,
    )
    return watchdog, state


def test_watchdog_recovery_resets_state(tmp_path, monkeypatch):
    watchdog, state = make_watchdog(tmp_path, monkeypatch)
    assert watchdog.tick() is False
    assert state["starts"] == 1
    state["healthy"] = True
    assert watchdog.tick() is True
    assert watchdog.down_since is None and watchdog.restart_attempted is False


def test_failed_restart_alerts_once_without_retry_loop(tmp_path, monkeypatch):
    watchdog, state = make_watchdog(tmp_path, monkeypatch)
    watchdog.tick()
    state["now"] = 300
    watchdog.tick()
    state["now"] = 600
    watchdog.tick()
    assert state["starts"] == 1
    alerts = [
        json.loads(line) for line in (tmp_path / "logs/alerts.jsonl").read_text().splitlines()
    ]
    assert len(alerts) == 1 and alerts[0]["category"] == "napcat_down"
    assert set(alerts[0]) == {"category", "timestamp"}


def test_recovery_then_new_outage_retries_once_and_resets_alert(tmp_path, monkeypatch):
    watchdog, state = make_watchdog(tmp_path, monkeypatch)
    watchdog.tick()
    state["now"] = 300
    watchdog.tick()
    assert state["starts"] == 1
    state["healthy"] = True
    assert watchdog.tick() is True
    state["healthy"] = False
    state["now"] = 301
    watchdog.tick()
    assert state["starts"] == 2
    assert watchdog.down_since == 301 and watchdog.alerted is False
