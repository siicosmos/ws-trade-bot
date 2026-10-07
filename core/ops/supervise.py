"""Thread supervision.

Background threads (updater, stop monitor, mirror) must never
die silently - a logic error that exits a loop would otherwise
leave the bot running with a feature quietly off. supervised()
relaunches the target after a crash *or* an unexpected return,
logs it, and reports to the webhook so it is never a surprise.
"""

import threading
import time


def supervised(
    name, target, webhook_url="", restart_delay=30, log=print
):
    """Run target() forever on a daemon thread.

    target is expected to loop internally; if it returns or
    raises, that is treated as a crash: it is logged, reported
    to the webhook, and retried after restart_delay seconds.
    Returns (thread, state) where state exposes restarts and
    the last error.
    """
    state = {"restarts": 0, "last_error": None, "thread": None}

    def _report(text):
        try:
            log(text)
        except Exception:
            pass
        if webhook_url:
            try:
                from core.ops.notify import notify_discord

                notify_discord(
                    webhook_url,
                    f"{name} thread",
                    {"status": text[:1000]},
                    ok=False,
                )
            except Exception:
                pass

    def _run():
        while True:
            try:
                target()
                state["restarts"] += 1
                _report(
                    f"{name} thread exited unexpectedly - "
                    f"restarting in {restart_delay}s"
                )
            except Exception as e:
                state["restarts"] += 1
                state["last_error"] = str(e)
                _report(
                    f"{name} thread crashed: {e} - "
                    f"restarting in {restart_delay}s"
                )
            time.sleep(restart_delay)

    t = threading.Thread(
        target=_run, daemon=True, name=name
    )
    t.start()
    state["thread"] = t
    return t, state
