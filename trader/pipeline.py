import hashlib

from .executor import ExecutionResult, account_sizing
from .notify import notify_alert, notify_discord
from .parser import parse_alert
from .risk import RiskEngine


def _message_key(text: str, author: str = "") -> str:
    payload = f"{author}:{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def process_alert(
    text, author, cfg, store, risk: RiskEngine, executor, account=None
) -> dict:
    if not text or not text.strip():
        return {"status": "ignored", "reason": "empty message"}

    key = _message_key(text, author)
    if store.seen_signal(key):
        return {"status": "ignored", "reason": "duplicate message"}

    alert = parse_alert(text, cfg.parser.custom_patterns)
    store.record_signal(key, author, text, alert is not None)

    if alert is None:
        return {"status": "ignored", "reason": "no actionable signal"}

    if cfg.trading.mode == "notify":
        sizing = (
            account_sizing(alert, cfg, account, store)
            if account and alert.action == "BUY"
            else []
        )
        notify_alert(cfg.discord.webhook_url, alert, sizing)
        return {
            "status": "notified",
            "alert": alert.to_dict(),
            "sizing": sizing,
        }

    if executor is None:
        return {"status": "error", "reason": "no executor configured"}

    allowed, reason = risk.evaluate(alert)
    if not allowed:
        notify_discord(
            cfg.discord.webhook_url,
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
            cfg.discord.webhook_url,
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

    notify_discord(
        cfg.discord.webhook_url,
        f"[{executor.mode.upper()}] {alert.action} {alert.ticker}",
        {
            "result": result.detail,
            "qty": result.qty,
            "entry": alert.entry or "-",
            "stop": alert.stop_loss or "-",
            "target": alert.take_profit or "-",
        },
        ok=result.ok,
    )

    return {
        "status": "executed" if result.ok else "skipped",
        "detail": result.detail,
        "alert": alert.to_dict(),
    }
