"""Signal identity + atomic claiming, shared by both apps.

The info server records signals and serves them as the alert
feed; consumer apps run the full process_alert on delivery.
Both paths claim a signal by inserting its message key first -
the INSERT OR IGNORE under the store's write lock is the atomic
dedupe, so dual delivery (push + feed pull) can race and only
one claim wins.
"""

import hashlib


def message_key(text: str, author: str = "") -> str:
    payload = f"{author}:{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def claim_signal(store, text, author, alert, correction, channel, ts,
                 parsed_ts):
    """Dedupe + record, atomically. Alerts and corrections are
    claimed by the INSERT itself (PK conflict = duplicate).
    Chatter keeps the old check-then-record flow (re-reads
    re-notify by design). Returns (key, claimed, reason) -
    reason only when not claimed."""
    key = message_key(text, author)
    is_signal = alert is not None or correction
    if is_signal and store.seen_signal(key):
        return key, False, "duplicate message"
    if store.has_recent_prefix(text):
        return key, False, "duplicate message (reaction re-read)"
    inserted = store.record_signal(
        key, author, text, alert is not None, correction=correction,
        channel=channel, ts_epoch=ts, parsed_epoch=parsed_ts,
    )
    if is_signal and not inserted:
        return key, False, "duplicate message"
    return key, True, None