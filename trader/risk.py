from datetime import date, datetime, timezone

from .store import Store


class RiskEngine:
    def __init__(self, cfg, store: Store, account=None):
        self.cfg = cfg
        self.store = store
        self.account = account

    def evaluate(self, alert) -> tuple:
        t = self.cfg.trading
        mode = "paper" if t.mode != "live" else "live"

        if t.ticker_whitelist and alert.ticker not in t.ticker_whitelist:
            return False, f"{alert.ticker} not in whitelist"

        if alert.action == "BUY":
            if t.max_trades_per_day > 0:
                if self.store.trades_today(mode) >= t.max_trades_per_day:
                    return False, "daily trade limit reached"

            if t.cooldown_seconds > 0:
                last = self.store.last_buy_time()
                if last:
                    elapsed = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(last)
                    ).total_seconds()
                    if elapsed < t.cooldown_seconds:
                        return False, (
                            f"cooldown active "
                            f"({int(t.cooldown_seconds - elapsed)}s left)"
                        )

            if t.max_consecutive_losses > 0:
                streak = self.store.loss_streak(mode)
                if streak >= t.max_consecutive_losses:
                    return False, (
                        f"loss-streak breaker: {streak} consecutive losing "
                        f"trades - buys resume tomorrow"
                    )

            if (
                t.min_dte_days > 0
                and alert.kind == "option"
                and alert.expiry
            ):
                try:
                    days = (
                        date.fromisoformat(alert.expiry) - date.today()
                    ).days
                except ValueError:
                    days = 0
                if days < t.min_dte_days:
                    return False, (
                        f"expiry {alert.expiry} is {days} DTE, "
                        f"below min_dte_days ({t.min_dte_days})"
                    )

        if t.dedupe_window_minutes > 0 and self.store.recent_trade(
            alert.dedupe_key(), t.dedupe_window_minutes
        ):
            return False, (
                f"duplicate signal within {t.dedupe_window_minutes}min"
            )

        return True, "ok"
