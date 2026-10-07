"""Main-thread liveness watchdog.

The pipeline's crash path is already covered (nonzero exit ->
the .bat restart loop). A hang is not: if the Flask accept loop
or worker pool wedges, the process stays up but serves nothing.
The watchdog fetches its own /health endpoint and exits the
process when serving has been dead past the grace window -
letting the restart loop do the recovery.
"""

import os
import time


def tick(state, ok, now, grace_seconds):
    """One health-check decision.

    Returns None (healthy or within grace), "arming" (first
    failure - the clock starts) or "exit" (grace elapsed, the
    process should be restarted)."""
    if ok:
        state["fail_since"] = None
        return None
    if state["fail_since"] is None:
        state["fail_since"] = now
        return "arming"
    if now - state["fail_since"] >= grace_seconds:
        return "exit"
    return None


def start_health_watchdog(
    health_url,
    webhook_url="",
    interval_seconds=30,
    grace_seconds=300,
    log=print,
    _exit=None,
    verify=True,
    store=None,
):
    """Returns the supervised thread. Fetches health_url every
    interval; after grace_seconds of consecutive failures the
    process exits 1 so the .bat loop restarts it."""
    import requests

    from core.ops.supervise import supervised

    _exit = _exit or os._exit
    state = {"fail_since": None}

    def check():
        while True:
            time.sleep(max(5, interval_seconds))
            # the daily history prune rides this always-on loop:
            # the mirror loop only runs when mirroring is enabled
            if store is not None:
                try:
                    store.maybe_prune()
                except Exception as e:
                    log(f"history prune failed: {e}")
            try:
                resp = requests.get(
                    health_url, timeout=10, verify=verify
                )
                ok = resp.status_code == 200
            except Exception:
                ok = False
            decision = tick(
                state, ok, time.time(), grace_seconds
            )
            if decision is None:
                continue
            if decision == "arming":
                log(
                    f"health watchdog: {health_url} unreachable - "
                    f"will exit after {grace_seconds}s if it stays "
                    f"down"
                )
                continue
            if decision == "exit":
                now = time.time()
                log(
                    "health watchdog: main thread not serving for "
                    f"{int(now - state['fail_since'])}s - exiting "
                    "for the restart loop to recover"
                )
                if webhook_url:
                    try:
                        from core.ops.notify import notify_discord

                        notify_discord(
                            webhook_url,
                            "Pipeline hung - restarting",
                            {
                                "reason": (
                                    "the dashboard stopped "
                                    "responding; the restart loop "
                                    "is recovering it"
                                )
                            },
                            ok=False,
                        )
                    except Exception:
                        pass
                _exit(1)

    thread, _ = supervised(
        "health-watchdog", check, webhook_url
    )
    return thread
