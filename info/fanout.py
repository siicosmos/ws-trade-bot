"""Push fan-out for the info server: deliver every newly
recorded signal to each registered consumer's /alert endpoint.

Delivery is best-effort with bounded retries - a consumer that
stays unreachable just shows red on the info dashboard; its
feed-pull cursor backfills anything it missed. State (per-
consumer last-seen, cursors, push results) is shared with the
feed API through FEED_STATE so one dashboard shows both paths.
"""

import threading
import time
from datetime import datetime, timezone

import requests

from core.ops.supervise import supervised

# how hard the push tries before leaving the alert to the
# consumer's pull cursor
PUSH_ATTEMPTS = 3
PUSH_RETRY_SECONDS = 2

_lock = threading.Lock()
FEED_STATE = {
    # per consumer label: last_seen (feed poll), cursor, push stats
    "consumers": {},
}


def _entry(label):
    entry = FEED_STATE.get("consumers").get(label)
    if entry is None:
        entry = {
            "label": label,
            "last_seen": None,
            "cursor": None,
            "pushed": 0,
            "push_failed": 0,
            "last_push_ts": None,
            "last_error": None,
        }
        FEED_STATE["consumers"][label] = entry
    return entry


def record_seen(label, cursor):
    """The feed API notes a consumer's poll."""
    with _lock:
        entry = _entry(label)
        entry["last_seen"] = time.time()
        entry["cursor"] = cursor


def record_push(label, ok, error=None):
    with _lock:
        entry = _entry(label)
        entry["last_push_ts"] = time.time()
        if ok:
            entry["pushed"] += 1
            entry["last_error"] = None
        else:
            entry["push_failed"] += 1
            entry["last_error"] = str(error)[:200] if error else "failed"


def snapshot():
    with _lock:
        return {
            "consumers": [
                dict(v) for v in FEED_STATE["consumers"].values()
            ]
        }


def _push(consumer, row, verify_ssl):
    """Deliver one signal in the reader's /alert payload shape."""
    try:
        ts = datetime.fromisoformat(row["ts"]).timestamp()
    except (TypeError, ValueError):
        ts = None
    payload = {
        "text": row["text"] or "",
        "author": row["author"] or "",
        "ts": ts,
        "parsed_ts": None,
        "channel": row["channel"] or "",
    }
    headers = {"X-Auth-Token": consumer.token}
    last_error = None
    for attempt in range(PUSH_ATTEMPTS):
        try:
            r = requests.post(
                consumer.push_url, json=payload, headers=headers,
                timeout=5, verify=verify_ssl,
            )
            if r.status_code < 300:
                return True, None
            last_error = f"HTTP {r.status_code}"
        except Exception as e:
            last_error = e
        if attempt < PUSH_ATTEMPTS - 1:
            time.sleep(PUSH_RETRY_SECONDS)
    return False, last_error


# one fan-out pass spends at most this much wall clock - a dead
# consumer (3 attempts x 5s timeout + retries ~= 19s per row) must
# not starve the healthy ones for minutes; the cut consumer's
# cursor stays where it stopped and the next tick (plus the pull
# cursor) backfills the rest
TICK_BUDGET_SECONDS = 30


def _tick(store, consumers, cursors):
    """One fan-out pass: push every signal past each consumer's
    cursor and advance it (even on failure - the pull cursor
    backfills)."""
    deadline = time.time() + TICK_BUDGET_SECONDS
    for c in consumers:
        if time.time() >= deadline:
            break
        # the cursor keys by label (the identity everywhere
        # else): two consumers sharing a token would otherwise
        # share one cursor and skip each other's rows
        rows = store.signals_since(cursors.get(c.label, 0), limit=50)
        if not rows:
            continue
        verify = bool(getattr(c, "push_verify_ssl", False))
        for row in rows:
            if time.time() >= deadline:
                break
            ok, err = _push(consumer=c, row=row,
                            verify_ssl=verify)
            record_push(c.label, ok, err)
            cursors[c.label] = row["id"]


def _loop(cfg, store, consumers):
    # start at the current head: restarts never re-push history,
    # consumers backfill via their pull cursor if needed
    cursors = {c.label: store.max_signal_rowid() for c in consumers}
    while True:
        time.sleep(0.5)
        _tick(store, consumers, cursors)


def start_fanout_thread(cfg, store, webhook_url=""):
    consumers = [
        c for c in (cfg.consumers or [])
        if c.push_url and c.token
    ]
    if not consumers:
        return None
    t, _state = supervised(
        "alert fan-out", lambda: _loop(cfg, store, consumers),
        webhook_url,
    )
    return t
