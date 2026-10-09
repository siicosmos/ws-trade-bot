import hashlib

from consumer.trading.executor import ExecutionResult, account_sizing
from core.ops.notify import (
    notify_alert,
    notify_correction,
    notify_discord,
    notify_plain,
)
from core.parser import is_correction, parse_alert
from consumer.trading.risk import RiskEngine


def _message_key(text: str, author: str = "") -> str:
    # the shared definition (core.signals) - the feed client's
    # backfill computes the same keys, so a divergence here would
    # silently break dedupe
    from core.signals import message_key
    return message_key(text, author)


def _paper_enabled(cfg):
    return bool(
        getattr(getattr(cfg, "paper", None), "enabled", False)
    )


def _notify_enabled(cfg):
    return bool(
        getattr(getattr(cfg, "discord", None), "notify", True)
    )


def _claim_signal(store, text, author, alert, correction, channel, ts,
                  parsed_ts):
    """Dedupe + record, atomically. Alerts and corrections are
    claimed by the INSERT itself (PK conflict = duplicate) - two
    delivery paths (push + feed pull) can race on the same
    message and only one may execute it. Chatter keeps the old
    check-then-record flow (re-reads re-notify by design).
    Returns (key, claimed, reason) - reason only when not
    claimed."""
    key = _message_key(text, author)
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


def process_alert(
    text, author, cfg, store, risk: RiskEngine, executor, account=None,
    channel: str = "", ts=None, parsed_ts=None,
) -> dict:
    if not text or not text.strip():
        return {"status": "ignored", "reason": "empty message"}

    alert = parse_alert(text, cfg.parser.custom_patterns)
    correction = is_correction(text)
    key, claimed, reason = _claim_signal(
        store, text, author, alert, correction, channel, ts, parsed_ts,
    )
    if not claimed:
        return {"status": "ignored", "reason": reason}

    mismatch = None
    if (
        alert is not None and alert.kind == "option"
        and alert.action == "SELL" and alert.strike is not None
        and alert.expiry
    ):
        try:
            prev = store.last_buy_contract(
                alert.ticker, alert.expiry
            )
        except Exception:
            prev = None
        if prev and (
            abs(prev["strike"] - alert.strike) > 1e-9
            or prev["right"] != alert.right
        ):
            mismatch = (
                f"sell {alert.strike:g}{alert.right} differs from "
                f"last buy {prev['strike']:g}{prev['right']}"
            )

    if alert is None:
        if correction:
            if _notify_enabled(cfg):
                notify_correction(cfg.discord.trade_alert_webhook_url, text)
            return {"status": "correction", "alert": None}
        if _notify_enabled(cfg):
            notify_plain(cfg.discord.trade_alert_webhook_url, text)
        return {"status": "ignored", "reason": "no actionable signal"}

    if cfg.trading.mode == "notify":
        sizing = (
            account_sizing(alert, cfg, account, store)
            if account and alert.action == "BUY"
            else []
        )
        if _notify_enabled(cfg):
            notify_alert(cfg.discord.trade_alert_webhook_url, alert, sizing,
                         correction=correction, mismatch=mismatch)
        # notify mode is the dry-run ledger: record what would have
        # been traded so the trade log stays meaningful
        parts = []
        for row in sizing or []:
            label = row.get("label", "?")
            contracts = row.get("contracts") or 0
            if contracts:
                parts.append(
                    f"{label}: buy {contracts} @ {alert.premium:g}"
                )
            else:
                warnings = row.get("warnings") or []
                parts.append(
                    f"{label}: skip "
                    f"({'; '.join(warnings) if warnings else 'no budget'})"
                )
        detail = "notify mode - " + (
            "; ".join(parts) if parts else "advisory only"
        )
        if mismatch:
            detail = "⚠ " + mismatch + " - " + detail
        if correction:
            detail = "correction - " + detail
        paper = None
        if _paper_enabled(cfg) and executor is not None:
            # paper trading alongside notify: execute against the
            # seeded ledger and record the simulated fill. The
            # same risk gates as paper mode apply (whitelist,
            # daily limit, cooldown, loss-streak breaker) -
            # the hybrid used to bypass them entirely.
            allowed, reason = risk.evaluate(alert)
            if allowed:
                try:
                    res = executor.execute(alert, cfg, store)
                except Exception as e:
                    # the main path records the error (which
                    # releases the execution claim) and notifies -
                    # the hybrid leg does the same
                    res = None
                    paper = {
                        "ok": False, "qty": 0,
                        "detail": f"error: {e}",
                    }
                    store.record_trade(
                        "paper", alert.action, alert.ticker, 0,
                        alert.premium, alert, "error", str(e), key,
                    )
                if res is not None:
                    paper = {
                        "ok": bool(res.ok),
                        "qty": res.qty,
                        "detail": res.detail,
                    }
                    store.record_trade(
                        "paper", alert.action, alert.ticker,
                        res.qty or 0, res.price or alert.premium,
                        alert,
                        "executed" if res.ok else "skipped",
                        (res.detail or "") + (
                            " | " + mismatch if mismatch else ""
                        ), key,
                    )
            else:
                paper = {
                    "ok": False,
                    "qty": 0,
                    "detail": f"blocked: {reason}",
                }
                store.record_trade(
                    "paper", alert.action, alert.ticker, 0,
                    alert.premium, alert, "skipped",
                    f"blocked: {reason}", key,
                )
            detail += " | paper: " + (paper["detail"] or "-")
        store.record_trade(
            "notify", alert.action, alert.ticker, 0, alert.premium,
            alert, "notified", detail, key,
        )
        return {
            "status": "notified",
            "alert": alert.to_dict(),
            "sizing": sizing,
            "correction": correction,
            "paper": paper,
        }

    if executor is None:
        return {"status": "error", "reason": "no executor configured"}

    allowed, reason = risk.evaluate(alert)
    if not allowed:
        notify_discord(
            cfg.discord.trade_alert_webhook_url,
            f"Signal blocked: {alert.action} {alert.ticker}",
            {"reason": reason, "alert": alert.raw[:200]},
            ok=False,
        )
        return {"status": "blocked", "reason": reason, "alert": alert.to_dict()}

    try:
        result: ExecutionResult = executor.execute(alert, cfg, store)
    except Exception as e:
        store.record_trade(
            executor.mode, alert.action, alert.ticker, 0, None,
            alert, "error", str(e), key,
        )
        notify_discord(
            cfg.discord.trade_alert_webhook_url,
            f"ERROR executing {alert.action} {alert.ticker}",
            {"error": str(e), "alert": alert.raw[:200]},
            ok=False,
        )
        return {"status": "error", "reason": str(e), "alert": alert.to_dict()}

    store.record_trade(
        executor.mode, alert.action, alert.ticker, result.qty,
        result.price, alert, "executed" if result.ok else "skipped",
        result.detail, key,
    )

    # the execution embed keeps the rich alert format (contract
    # details + per-account sizing lines) with the mode tag in
    # the title - the bare one-liner crammed everything into a
    # single result field
    exec_sizing = (
        account_sizing(alert, cfg, account, store)
        if account and alert.action == "BUY" else []
    )
    extra = {"result": result.detail}
    if result.ok and result.qty:
        extra["filled"] = f"**{result.qty}**"
    if alert.entry:
        extra["entry"] = alert.entry
    if alert.stop_loss:
        extra["stop"] = alert.stop_loss
    if alert.take_profit:
        extra["target"] = alert.take_profit
    if correction:
        extra["note"] = "ADMIN CORRECTION - may supersede the previous alert"
    notify_alert(
        cfg.discord.trade_alert_webhook_url, alert, sizing=exec_sizing,
        correction=correction, mismatch=mismatch,
        prefix=f"[{executor.mode.upper()}] ", extra_fields=extra,
        # the box color follows the action - sells are red, buys
        # green - the result field carries the outcome
    )

    return {
        "status": "executed" if result.ok else "skipped",
        "detail": result.detail,
        "alert": alert.to_dict(),
        "correction": correction,
    }


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
    key, claimed, reason = _claim_signal(
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
