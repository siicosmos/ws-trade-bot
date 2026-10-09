"""Thread supervision: the crash report must identify the
failure (exception type + message + traceback) - after
restart-delay cycles, "connection reset" alone says nothing
about which target() call failed or where."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ops import supervise  # noqa: E402


def test_supervised_logs_exception_type_and_traceback():
    seen = []
    stop = {"stop": False}

    def _boom():
        if stop["stop"]:
            # deterministic teardown: the next crash exits via the
            # deliberate-exit branch instead of looping forever
            raise SystemExit
        raise ValueError("kaboom from the test")

    t, state = supervise.supervised(
        "test-boom", _boom, restart_delay=0.01,
        log=seen.append,
    )
    deadline = time.time() + 2
    while time.time() < deadline and not any(
        "Traceback" in s for s in seen
    ):
        time.sleep(0.01)
    joined = "\n".join(seen)
    # type and message lead (the webhook's [:1000] truncation
    # must never drop them), the traceback follows
    assert "test-boom thread crashed: ValueError: kaboom" in joined
    assert "Traceback" in joined
    assert "test_supervise.py" in joined   # the failing frame
    assert state["last_error"].startswith("ValueError")
    assert state["restarts"] >= 1
    # tear down: the next crash exits deliberately
    stop["stop"] = True
    t.join(timeout=2)
    assert not t.is_alive()


def test_supervised_reports_unexpected_return():
    """A target that returns (instead of looping) is restarted -
    no traceback exists for a return, so the message stays as it
    was."""
    seen = []
    stop = {"stop": False}

    def _quit():
        if stop["stop"]:
            raise SystemExit
        # returning immediately: the "exited unexpectedly" path

    t, state = supervise.supervised(
        "test-quit", _quit, restart_delay=0.01,
        log=seen.append,
    )
    deadline = time.time() + 2
    while time.time() < deadline and not any(
        "exited unexpectedly" in s for s in seen
    ):
        time.sleep(0.01)
    joined = "\n".join(seen)
    assert "test-quit thread exited unexpectedly" in joined
    assert "Traceback" not in joined
    stop["stop"] = True
    t.join(timeout=2)
    assert not t.is_alive()


def test_supervised_deliberate_exit_has_no_traceback():
    """SystemExit / KeyboardInterrupt are deliberate - the report
    names the exit without a traceback (unchanged behavior)."""
    seen = []

    def _exit():
        raise SystemExit("done")

    t, state = supervise.supervised(
        "test-exit", _exit, restart_delay=0.01,
        log=seen.append,
    )
    t.join(timeout=2)
    joined = "\n".join(seen)
    assert "test-exit thread exited deliberately: done" in joined
    assert "Traceback" not in joined
    assert not t.is_alive()
