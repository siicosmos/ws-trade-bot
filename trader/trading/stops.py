import threading
import time

from ..ops.notify import notify_discord
from .parser import Alert


class StopMonitor:
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

    def stop_price(self, entry, peak):
        t = self.cfg.trading
        if not entry or entry <= 0:
            return None
        stop = entry * (1 - t.stop_loss_pct / 100.0)
        if t.trailing_stop_pct > 0 and peak and peak > entry:
            trail = peak * (1 - t.trailing_stop_pct / 100.0)
            if trail > stop:
                return trail
        return stop

    def check_once(self):
        self.last_run = time.time()
        t = self.cfg.trading
        if t.stop_loss_pct <= 0:
            return
        for pos in self.store.list_positions(self.trader.mode):
            try:
                bid = self.quote_fn(pos)
            except Exception:
                bid = None
            if not bid or bid <= 0:
                continue
            self.store.update_peak_bid(
                self.trader.mode, pos["contract_key"], bid, pos["account"]
            )
            peak = max(
                bid,
                pos.get("peak_bid") or 0,
                pos.get("avg_premium") or 0,
            )
            stop = self.stop_price(pos.get("avg_premium"), peak)
            if stop is None or bid > stop:
                continue
            self._fire(pos, bid)

    def _fire(self, pos, bid):
        alert = Alert(
            action="SELL",
            ticker=pos["underlying"],
            kind="option",
            underlying=pos["underlying"],
            expiry=pos["expiry"],
            strike=pos["strike"],
            right=pos["right"],
            premium=bid,
            raw=(
                f"[STOP] auto stop-loss on {pos['contract_key']} "
                f"at {bid} (entry ~{pos.get('avg_premium')})"
            ),
        )
        try:
            result = self.trader.execute(alert, self.cfg, self.store)
        except Exception as e:
            self.store.record_trade(
                self.trader.mode, "SELL", pos["underlying"], 0, bid,
                alert, "error", f"[STOP] failed: {e}",
            )
            notify_discord(
                self.webhook_url,
                f"STOP LOSS FAILED: {pos['contract_key']}",
                {"error": str(e), "bid": bid},
                ok=False,
            )
            return

        self.store.record_trade(
            self.trader.mode, "SELL", pos["underlying"], result.qty, bid,
            alert, "executed" if result.ok else "stop_failed",
            f"[STOP] {result.detail}",
        )
        notify_discord(
            self.webhook_url,
            f"STOP LOSS HIT: {pos['contract_key']}",
            {
                "bid": bid,
                "entry": pos.get("avg_premium"),
                "sold": result.detail,
            },
            ok=False,
        )
