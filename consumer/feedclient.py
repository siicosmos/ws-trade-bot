"""Consumer-side feed client: pull alerts from the info server.

The consumer app's alert source - it long-polls the info
server's /api/feed and feeds every alert into the same
process_alert the /alert route uses. Push (the server POSTing
here) and pull can run at once: the atomic signal claim makes
dual delivery idempotent.

First start anchors at the feed head - history is never
replayed (an old BOUGHT alert must not execute now).
"""

import time
from datetime import datetime

import requests

from core.ops.supervise import supervised

# backoff on feed errors: seconds between polls after failures
BACKOFF_MIN = 2.0
BACKOFF_MAX = 30.0


def _epoch(iso_ts):
    try:
        return datetime.fromisoformat(iso_ts).timestamp()
    except (TypeError, ValueError):
        return None


def _loop(cfg, store, on_alert, state=None):
    base = cfg.feed.url.rstrip("/")
    headers = {"X-Auth-Token": cfg.feed.token}
    verify = bool(getattr(cfg.feed, "verify_ssl", False))
    poll = max(float(getattr(cfg.feed, "poll_seconds", 1.0) or 1.0), 0.5)
    cursor = None
    backoff = BACKOFF_MIN
    while True:
        try:
            if cursor is None:
                # anchor at the feed head - no history replay
                r = requests.get(
                    f"{base}/api/feed", headers=headers, timeout=10,
                    verify=verify,
                )
                if r.status_code == 401:
                    raise RuntimeError(
                        "feed token rejected (401) - feed.token must "
                        "match a consumers[] entry on the info server"
                    )
                r.raise_for_status()
                cursor = int(r.json()["cursor"])
                backoff = BACKOFF_MIN
                if state is not None:
                    state["last_seen"] = time.time()
                    state["ok"] = True
                    state["cursor"] = cursor
            else:
                r = requests.get(
                    f"{base}/api/feed",
                    params={"since": cursor, "wait": 25},
                    headers=headers, timeout=35, verify=verify,
                )
                if r.status_code == 401:
                    raise RuntimeError(
                        "feed token rejected (401) - feed.token must "
                        "match a consumers[] entry on the info server"
                    )
                r.raise_for_status()
                data = r.json()
                cursor = int(data["cursor"])
                backoff = BACKOFF_MIN
                if state is not None:
                    state["last_seen"] = time.time()
                    state["ok"] = True
                    state["cursor"] = cursor
                levels = data.get("levels")
                if levels and levels != store.meta_get(
                    "spx_levels_text"
                ):
                    store.meta_set("spx_levels_text", levels)
                for a in data.get("alerts") or []:
                    try:
                        on_alert(
                            a.get("text") or "",
                            a.get("author") or "",
                            _epoch(a.get("ts")),
                            a.get("channel") or "",
                        )
                    except Exception:
                        # one bad alert must not kill the feed
                        pass
        except Exception as e:
            if state is not None:
                state["ok"] = False
                state["error"] = str(e)[:200]
            # the failure used to be fully silent: a wrong feed
            # token (401) left the dashboard's reader line offline
            # with no reason in the log or the ui. the growing
            # backoff rate-limits the log naturally
            print(f"feed client: {e} - retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        time.sleep(poll)


def start_feed_client(cfg, store, on_alert, webhook_url="",
                      state=None):
    """on_alert(text, author, ts_epoch, channel) - usually a
    process_alert call. state (optional dict) tracks liveness
    for the dashboard's status line. Returns the thread or None
    when no feed is configured."""
    if not getattr(cfg, "feed", None) or not cfg.feed.url:
        return None
    if not cfg.feed.token:
        return None
    t, _state = supervised(
        "feed client", lambda: _loop(cfg, store, on_alert, state),
        webhook_url,
    )
    return t
