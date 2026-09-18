from datetime import datetime, timezone

from .store import Store


class RiskEngine:
    def __init__(self, cfg, store: Store, account=None):
        self.cfg = cfg
        self.store = store
        self.account = account

    def evaluate(self, alert) -> tuple:
        t = self.cfg.trading
        mode = "paper" if t.dry_run else "live"

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

            if t.max_open_risk_pct > 0 and self.account is not None:
                account_value = self.account.value()
                open_risk = self.store.open_risk(mode)
                limit = account_value * (t.max_open_risk_pct / 100.0)
                if open_risk >= limit:
                    return False, (
                        f"open risk {open_risk:.0f} already at "
                        f"{t.max_open_risk_pct}% cap ({limit:.0f})"
                    )

        if t.dedupe_window_minutes > 0 and self.store.recent_trade(
            alert.dedupe_key(), t.dedupe_window_minutes
        ):
            return False, (
                f"duplicate signal within {t.dedupe_window_minutes}min"
            )

        return True, "ok"
