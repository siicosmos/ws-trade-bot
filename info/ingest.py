"""Info-server ingest: parse + dedupe + record the signal -
no execution, no notifications. The recorded signal is what
the feed serves to consumer apps (they run the full
process_alert on their side)."""

import threading

from core.parser import is_correction, parse_alert
from core.signals import claim_signal

# woken on every recorded signal - the feed's long-poll waits on
# this instead of sleep-polling the db (which held a wsgi thread
# with ~80 queries per waiting request)
FEED_WAKE = threading.Condition()


def ingest_alert(
    text, author, cfg, store, channel: str = "", ts=None, parsed_ts=None,
) -> dict:
    """Info-server ingest: parse + dedupe + record the signal -
    no execution, no notifications. The recorded signal is what
    the feed serves to consumer apps (they run the full
    process_alert on their side)."""
    if not text or not text.strip():
        return {"status": "ignored", "reason": "empty message"}

    alert = parse_alert(text, cfg.parser.custom_patterns)
    correction = is_correction(text)
    key, claimed, reason = claim_signal(
        store, text, author, alert, correction, channel, ts, parsed_ts,
    )
    if not claimed:
        return {"status": "ignored", "reason": reason}
    with FEED_WAKE:
        FEED_WAKE.notify_all()
    return {
        "status": "recorded",
        "alert": alert.to_dict() if alert is not None else None,
        "correction": correction,
        "key": key,
    }
