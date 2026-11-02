"""P0 known-difference cases for the code-review (梳理) findings KD-S01/KD-S02.

Registered in ``tests/KNOWN_DIFFERENCES_P0.md``. Both cases assert the
contract behaviour; they ran as ``xfail(strict=True)`` at P0 and were fixed
in P3, so they now run as plain regression tests (xfail retired in the
registry as part of the P3 closure).

KD-S03 (「忘掉」does not cover care data) was registered as a
document-level difference at P0 because there was no ingress test
surface; the P6 care wiring created that surface and closed it — see
the registry and tests/test_care_wiring_p6.py.
"""

import asyncio

import pytest

from companion.safety import crisis_reply
from tests import test_app
from tests.fake_onebot import private_event
from tests.test_current_time import registry as time_registry

setup_app = test_app.setup_app


class ExplodingJudge:
    """A judge whose failure is an ordinary exception, not JudgeUnavailable."""

    async def judge(self, _text):
        raise RuntimeError("synthetic judge failure")


@pytest.mark.timeout(10)
def test_kd_s01_ordinary_judge_exception_is_suspected_crisis(setup_app):
    app, calls, _, settings = setup_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.safety.judge = ExplodingJudge()
    assert asyncio.run(processor.handle(private_event(text="今天心情一般")))
    # Contract expectation: a judge that fails with an ordinary exception is
    # treated like JudgeUnavailable — suspected crisis, fixed crisis copy.
    assert calls[-1]["message"] == crisis_reply(
        settings.safety["helplines"], False, settings.safety
    )


@pytest.mark.timeout(10)
def test_kd_s02_ambiguous_time_clarify_writes_no_memory(setup_app):
    from companion.tools.current_time import CLARIFY_REPLY

    app, calls, _, _ = setup_app
    processor = app.state.processor
    asyncio.run(processor.conversation.mark_onboarded("10001"))
    processor.read_only_registry = time_registry(
        lambda: pytest.fail("ambiguous request must not call the clock")
    )
    assert asyncio.run(processor.handle(private_event(text="几点")))
    # Preliminary proof the clarify branch was actually taken.
    assert calls[-1]["message"] == CLARIFY_REPLY
    # Contract expectation: like the direct time route, no memory is written.
    assert asyncio.run(processor.conversation.get_history("10001")) == []
