from datetime import datetime, timezone

from .store import Store


class RiskEngine:
    def __init__(self, cfg, store: Store):
        self.cfg = cfg
        self.store = store

    def evaluate(self, alert) -> tuple:
        t = self.cfg.trading

        if t.ticker_whitelist and alert.ticker not in t.ticker_whitelist:
            return False, f"{alert.ticker} not in whitelist"

        if t.max_trades_per_day > 0:
            mode = "paper" if t.dry_run else "live"
            if self.store.trades_today(mode) >= t.max_trades_per_day:
                return False, "daily trade limit reached"

        if t.cooldown_seconds > 0:
            last = self.store.last_trade_time()
            if last:
                elapsed = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(last)
                ).total_seconds()
                if elapsed < t.cooldown_seconds:
                    return False, (
                        f"cooldown active ({int(t.cooldown_seconds - elapsed)}s left)"
                    )

        if t.dedupe_window_minutes > 0 and self.store.recent_trade(
            alert.ticker, alert.action, t.dedupe_window_minutes
        ):
            return False, (
                f"duplicate {alert.action} {alert.ticker} within "
                f"{t.dedupe_window_minutes}min"
            )

        if alert.action == "SELL" and t.sell_only_if_held:
            pass

        return True, "ok"
