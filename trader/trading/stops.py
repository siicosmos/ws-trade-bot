import datetime as _dt
import threading
import time

from ..ops.notify import notify_discord
from .parser import Alert


class StopMonitor:
    """Watches open option positions and sells them when their
    stop is hit.

    The stop is entry x (1 - stop_loss_pct) with an optional
    trailing floor above the peak bid; both knobs come from the
    position's size tier when it configures its own values (a
    lotto play tolerates -50%, a full-size play only -20%).
    On a 0dte expiry day a position that had a gain and gave it
    back to its entry is sold before it expires worthless."""

    def __init__(self, cfg, store, trade_executor, quote_fn, webhook_url=""):
        self.cfg = cfg
        self.store = store
        self.trader = trade_executor
        self.quote_fn = quote_fn
        self.webhook_url = webhook_url
        self._thread = None
        self.last_run = None
        self.errors = 0

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            from ..ops.supervise import supervised

            self._thread, _ = supervised(
                "stop-monitor", self._run, self.webhook_url
            )

    def _run(self):
        while True:
            time.sleep(max(5, int(self.cfg.trading.stop_check_seconds)))
            try:
                self.check_once()
            except Exception as e:
                self.errors += 1
                print(f"stop monitor error: {e}")

    def stop_price(self, entry, peak, pos=None):
        """The effective stop: the per-size stop loss from the
        position's tier when configured, the global one otherwise;
        a trailing stop above the peak bid ratchets over it."""
        t = self.cfg.trading
        if not entry or entry <= 0:
            return None
        tier = self._tier_for(pos)
        stop_pct = float(t.stop_loss_pct)
        if tier is not None and tier.get("stop_loss_pct") is not None:
            stop_pct = float(tier["stop_loss_pct"])
        stop = entry * (1 - stop_pct / 100.0)
        if t.trailing_stop_pct > 0 and peak and peak > entry:
            trail = peak * (1 - t.trailing_stop_pct / 100.0)
            if trail > stop:
                return trail
        return stop

    def _tier_for(self, pos):
        t = self.cfg.trading
        size = str((pos or {}).get("size") or "").lower()
        return (t.size_tiers or {}).get(size) if size else None

    def _back_to_entry_hit(self, pos, entry, peak, bid):
        """0dte option that had a gain and gave it all back - sell
        before it expires worthless. The lotto tier opts in by
        default; the global back_to_entry_enabled flag gates it."""
        t = self.cfg.trading
        if not getattr(t, "back_to_entry_enabled", True):
            return False
        if not entry or entry <= 0:
            return False
        expiry = str(pos.get("expiry") or "")[:10]
        if expiry != _dt.date.today().isoformat():
            return False   # not an expiry-day position
        if not peak or peak <= entry:
            return False   # never had a gain to protect
        tier = self._tier_for(pos)
        if tier is not None and tier.get("back_to_entry") is False:
            return False
        return bid <= entry

    def check_once(self):
        self.last_run = time.time()
        t = self.cfg.trading
        if t.stop_loss_pct <= 0:
            return
        for pos in self.store.list_positions(self.trader.mode):
            if not pos.get("right"):
                continue   # the stop monitor watches options only
            try:
                bid = self.quote_fn(pos)
            except Exception:
                bid = None
            if not bid or bid <= 0:
                continue
            self.store.update_peak_bid(
                self.trader.mode, pos["contract_key"], bid, pos["account"]
            )
            entry = pos.get("avg_premium") or 0
            peak = max(bid, pos.get("peak_bid") or 0, entry)
            stop = self.stop_price(entry, peak, pos)
            if stop is not None and bid <= stop:
                self._fire(pos, bid, reason="stop")
                continue
            # back to entry: a 0dte position that had a gain and
            # gave it all back is sold before it expires worthless
            if self._back_to_entry_hit(pos, entry, peak, bid):
                self._fire(pos, bid, reason="back_to_entry")

    def _fire(self, pos, bid, reason="stop"):
        tag = "[STOP]" if reason == "stop" else "[B2E]"
        title = (
            "BACK TO ENTRY FAILED" if reason == "back_to_entry"
            and False else ""
        )
        alert = Alert(
            action="SELL",
            ticker=pos["underlying"],
            kind="option",
            underlying=pos["underlying"],
            expiry=pos["expiry"],
            strike=pos["strike"],
            right=pos["right"],
            premium=bid,
            size=pos.get("size"),
            raw=(
                f"{tag} auto {reason.replace('_', ' ')} on "
                f"{pos['contract_key']} at {bid} "
                f"(entry ~{pos.get('avg_premium')})"
            ),
        )
        try:
            result = self.trader.execute(alert, self.cfg, self.store)
        except Exception as e:
            self.store.record_trade(
                self.trader.mode, "SELL", pos["underlying"], 0, bid,
                alert, "error", f"{tag} failed: {e}",
            )
            notify_discord(
                self.webhook_url,
                f"{tag} FAILED: {pos['contract_key']}",
                {"error": str(e), "bid": bid},
                ok=False,
            )
            return

        self.store.record_trade(
            self.trader.mode, "SELL", pos["underlying"], result.qty, bid,
            alert, "executed" if result.ok else "stop_failed",
            f"{tag} {result.detail}",
        )
        notify_discord(
            self.webhook_url,
            f"{tag} HIT: {pos['contract_key']}",
            {
                "bid": bid,
                "entry": pos.get("avg_premium"),
                "sold": result.detail,
            },
            ok=False,
        )
