from datetime import date, datetime, timezone

from core.store import Store


class RiskEngine:
    def __init__(self, cfg, store: Store, account=None):
        self.cfg = cfg
        self.store = store
        self.account = account

    def evaluate(self, alert) -> tuple:
        t = self.cfg.trading
        mode = "paper" if t.mode != "live" else "live"

        # the kill switch: a runtime toggle (no restart needed)
        # that blocks every new BUY - entries only, exits stay
        # allowed so stops and alert sells keep protecting
        if getattr(t, "trading_paused", False) and alert.action == "BUY":
            return False, "trading paused (kill switch) - buys blocked"

        if t.ticker_whitelist and alert.ticker not in t.ticker_whitelist:
            return False, f"{alert.ticker} not in whitelist"

        if alert.action == "BUY":
            if t.max_trades_per_day > 0:
                if self.store.trades_today(mode) >= t.max_trades_per_day:
                    return False, "daily trade limit reached"

            if t.cooldown_seconds > 0:
                last = self.store.last_buy_time(mode)
                if last:
                    elapsed = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(last)
                    ).total_seconds()
                    if elapsed < t.cooldown_seconds:
                        return False, (
                            f"cooldown active "
                            f"({int(t.cooldown_seconds - elapsed)}s left)"
                        )

            # the loss-streak breaker gates OPTIONS only - stock
            # alerts stay takeable (the streak itself clears the
            # next day)
            if t.max_consecutive_losses > 0 and alert.kind == "option":
                streak = self.store.loss_streak(mode)
                if streak >= t.max_consecutive_losses:
                    return False, (
                        f"loss-streak breaker: {streak} consecutive losing "
                        f"trades - option buys resume tomorrow"
                    )

            # the hard daily-loss breaker gates every BUY (options
            # and stocks): once today's realized pnl sinks below
            # the cap the day is done for entries - exits (alert
            # sells, stops, back-to-entry) stay allowed
            if t.max_daily_loss_pct > 0 and self.account is not None:
                try:
                    total = sum(
                        v for v in (self.account.values() or {}).values()
                        if v
                    )
                except Exception:
                    total = None
                if total and total > 0:
                    floor = -total * t.max_daily_loss_pct / 100.0
                    realized = self.store.realized_today(mode)
                    if realized <= floor:
                        return False, (
                            f"daily loss limit reached ({realized:,.2f} "
                            f"realized today, cap {floor:,.2f}) - "
                            f"new buys resume tomorrow"
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
