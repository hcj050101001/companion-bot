"""Shared test safeguards."""

import faulthandler
import signal

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "timeout(seconds): per-test timeout")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    marker = item.get_closest_marker("timeout")
    timeout = marker.args[0] if marker and marker.args else 10
    if hasattr(signal, "SIGALRM"):
        faulthandler.enable()
        previous = signal.signal(signal.SIGALRM, _timeout)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    else:
        yield


def _timeout(_signum, _frame):
    pytest.fail("test exceeded its 10 second timeout", pytrace=False)
