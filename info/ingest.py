"""Info-server ingest: parse + dedupe + record the signal -
no execution, no notifications. The recorded signal is what
the feed serves to consumer apps (they run the full
process_alert on their side)."""

from core.parser import is_correction, parse_alert
from core.signals import claim_signal


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
    return {
        "status": "recorded",
        "alert": alert.to_dict() if alert is not None else None,
        "correction": correction,
        "key": key,
    }
