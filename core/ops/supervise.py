"""Thread supervision.

Background threads (updater, stop monitor, mirror) must never
die silently - a logic error that exits a loop would otherwise
leave the bot running with a feature quietly off. supervised()
relaunches the target after a crash *or* an unexpected return,
logs it, and reports to the webhook so it is never a surprise.
"""

import threading
import time
import traceback


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
            except (KeyboardInterrupt, SystemExit) as e:
                # a deliberate exit must not loop-restart (the
                # interpreter only delivers KeyboardInterrupt to
                # the main thread, but a worker can raise
                # SystemExit itself)
                state["last_error"] = str(e)
                _report(f"{name} thread exited deliberately: {e}")
                return
            except Exception as e:
                state["restarts"] += 1
                # type + message lead the report: the webhook
                # truncates at 1000 chars and the traceback follows
                # - the identification must survive the cut (the
                # console/file log gets the whole thing)
                tb = traceback.format_exc()
                state["last_error"] = f"{type(e).__name__}: {e}"
                _report(
                    f"{name} thread crashed: "
                    f"{type(e).__name__}: {e}\n"
                    f"{tb}"
                    f"- restarting in {restart_delay}s"
                )
            time.sleep(restart_delay)

    t = threading.Thread(
        target=_run, daemon=True, name=name
    )
    t.start()
    state["thread"] = t
    return t, state
