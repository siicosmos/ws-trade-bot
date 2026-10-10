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
from core.redact import format_error, short_error

# backoff on feed errors: seconds between polls after failures
BACKOFF_MIN = 2.0
BACKOFF_MAX = 30.0


def _epoch(iso_ts):
    try:
        return datetime.fromisoformat(iso_ts).timestamp()
    except (TypeError, ValueError):
        return None


def _backfill_signals(base, store, limit=50, headers=None,
                      verify=True):
    """Pull the info server's recent signals into the consumer's
    signals table as ALREADY-CLAIMED rows: the dashboard's recent
    alerts survive a wiped/rebuilt consumer db, and the dedupe
    memory covers anything the info server still holds. Claimed
    without executing - old alerts must never re-trade. the rows
    insert OLDEST FIRST (the api returns newest first, reversed
    here) so the rowid order matches the time order and the
    dashboard shows newest on top."""
    try:
        r = requests.get(
            f"{base}/api/signals?limit={limit}",
            headers=headers, timeout=10, verify=verify,
        )
        r.raise_for_status()
        data = r.json()
        rows = list(reversed(data)) if isinstance(data, list) else []
    except Exception as e:
        # the token rides in the headers - an exception message
        # must never carry it into the log (the log tail is
        # posted to discord verbatim); format_error redacts the
        # traceback too (its last line repeats the message)
        print(f"feed backfill failed: "
              f"{format_error(e, (headers or {}).get('X-Auth-Token'))}")
        return 0
    added = 0
    for row in rows:
        try:
            ts = row.get("ts")
            ts_epoch = (
                datetime.fromisoformat(ts).timestamp() if ts else None
            )
            text = row.get("text") or ""
            if not text:
                continue
            # the info's /api/signals does not return message keys -
            # compute it locally (the same sha256 the claim uses)
            from core.signals import message_key

            key = message_key(text, row.get("author") or "")
            if store.record_signal(
                key, row.get("author") or "", text,
                bool(row.get("parsed")),
                correction=bool(row.get("correction")),
                channel=row.get("channel") or "",
                ts_epoch=ts_epoch,
            ):
                added += 1
        except Exception:
            continue
    if added:
        print(f"feed client: backfilled {added} recent alerts "
              f"from the info server")
    return added


def _loop(cfg, store, on_alert, state=None):
    base = cfg.feed.url.rstrip("/")
    headers = {"X-Auth-Token": cfg.feed.token}
    verify = bool(getattr(cfg.feed, "verify_ssl", True))
    if not verify:
        print(
            "feed client: TLS certificate verification is DISABLED "
            "(feed.verify_ssl: false) - connections are vulnerable "
            "to man-in-the-middle attacks; set feed.verify_ssl: true "
            "once the info server has a verifiable certificate"
        )
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
                    state.pop("error", None)
                # a fresh/wiped consumer db: pull the info server's
                # recent signals in as claimed rows - the dashboard's
                # recent alerts list is not empty and the dedupe
                # memory covers the info server's window
                _backfill_signals(
                    base, store, headers=headers, verify=verify
                )
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
                    state.pop("error", None)
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
                # the dashboard's reader line renders one line -
                # type + message there
                state["error"] = short_error(e, cfg.feed.token)[:200]
            # the failure used to be fully silent: a wrong feed
            # token (401) left the dashboard's reader line offline
            # with no reason in the log or the ui. the growing
            # backoff rate-limits the log naturally - and the line
            # stays one line: a long-poll reset is routine (every
            # info restart drops every poller), a traceback per
            # retry would flood the log the webhook ships
            print(f"feed client: {short_error(e, cfg.feed.token)} "
                  f"- retrying in {backoff:.0f}s")
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
