import datetime as _dt
import threading
import time

from core.ops.notify import notify_discord
from core.parser import Alert
from core.redact import format_error
from core.store import et_now


def atr_stop_price(provider, pos, peak, t):
    """The ATR trailing floor for one option position: the
    UNDERLYING's daily ATR (from the moomoo provider's cached
    daily bars) scaled by the option's delta, subtracted from
    the peak bid. Volatility-adaptive where the percentage trail
    is fixed - wide in stressed markets, tight in calm ones.

    None when disabled, when the position cannot be priced (no
    bars, no peak, or a zero delta), or on expiry day - a 0dte
    delta is the binary intrinsic step, so the atr floor adds no
    signal over the percentage trail and the back-to-entry
    guard; the caller falls back to the percentage trail
    (whichever is tighter wins)."""
    if not getattr(t, "atr_trailing", False):
        return None
    if provider is None or not hasattr(provider, "daily_bars"):
        return None
    if peak is None or peak <= 0:
        return None
    # expiry day: the delta step is binary (0 or 1) - the atr
    # floor would be either absent or the full underlying range
    from consumer.trading.greeks import days_to_expiry

    if days_to_expiry(pos.get("expiry")) == 0:
        return None
    period = int(getattr(t, "atr_period", 14) or 14)
    bars = provider.daily_bars(pos.get("underlying"), period)
    if not bars or len(bars) < 2:
        return None
    trs = []
    for prev, cur in zip(bars, bars[1:]):
        trs.append(max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        ))
    atr = sum(trs[-period:]) / min(period, len(trs))
    if atr <= 0:
        return None
    # the option's delta scales the underlying's range into the
    # option's expected range - from the store's own greeks math
    # (flat-iv approximation; the real spot rides the last close)
    from consumer.trading.greeks import bs_greeks

    spot = bars[-1]["close"]
    g = bs_greeks(
        spot, pos.get("strike"),
        days_to_expiry(pos.get("expiry")) / 365.0,
        0.05, 0.30, pos.get("right"),
    )
    delta = abs(g["delta"])
    if delta <= 0:
        return None
    k = float(getattr(t, "atr_multiplier", 2.0) or 2.0)
    return max(0.01, peak - k * delta * atr)


def adaptive_trail_pct(t, pos, peak, entry):
    """The effective trail distance for a position: the
    position's own trail_pct when pinned in the ui (0 = off for
    it), otherwise the stepped ratchet - the trail tightens as
    the gain at the peak grows (ride the run-up wide, lock in
    more near the top) and on 0dte expiry days tightens through
    the session. Returns None when trailing is off."""
    per_trail = (pos or {}).get("trail_pct") if pos else None
    if per_trail is not None:
        per_trail = float(per_trail)
        return per_trail if per_trail > 0 else None
    base = float(t.trailing_stop_pct)
    if not getattr(t, "adaptive_trail", False):
        return base if base > 0 else None
    if not peak or not entry or peak <= entry:
        return base if base > 0 else None

    gain = (peak / entry - 1.0) * 100.0
    steps = getattr(t, "adaptive_trail_steps", None) or {}
    trail = base if base > 0 else None
    for ceiling_key in sorted(steps, key=float):
        if gain <= float(ceiling_key):
            trail = float(steps[ceiling_key])
            break

    if trail is None:
        return None
    # 0dte: tighten through the expiry-day session (the air
    # pocket before the close)
    tighten = float(
        getattr(t, "adaptive_trail_expiry_tighten", 0) or 0
    )
    expiry = str((pos or {}).get("expiry") or "")[:10]
    if tighten and expiry == et_now().date().isoformat():
        now_et = et_now()
        hours_past = now_et.hour + now_et.minute / 60.0 - 13.0
        if hours_past > 0:
            trail -= tighten * hours_past
    return max(
        float(getattr(t, "adaptive_trail_min_pct", 0) or 0), trail
    )


class StopMonitor:
    """Watches open option positions and sells them when their
    stop is hit.

    The stop is entry x (1 - stop_loss_pct) with an optional
    trailing floor above the peak bid; both knobs come from the
    position's size tier when it configures its own values (a
    lotto play tolerates -50%, a full-size play only -20%).
    On a 0dte expiry day a position that had a gain and gave it
    back to its entry is sold before it expires worthless."""

    def __init__(self, cfg, store, trade_executor, quote_fn, webhook_url="",
                 provider=None):
        self.cfg = cfg
        self.store = store
        self.trader = trade_executor
        self.quote_fn = quote_fn
        self.provider = provider
        self.webhook_url = webhook_url
        self._thread = None
        self.last_run = None
        self.errors = 0
        self._delta_warn_ts = 0.0

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            from core.ops.supervise import supervised

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
                print(f"stop monitor error: {format_error(e)}")

    def stop_price(self, entry, peak, pos=None):
        """The effective stop: the per-size stop loss from the
        position's tier when configured, the global one otherwise;
        a trailing stop above the peak bid ratchets over it. The
        trailing distance is the position's own trail_pct when it
        carries one (0 = off for this position), the global
        trailing_stop_pct otherwise - so a per-position trailing
        works even with the global stops off."""
        t = self.cfg.trading
        if not entry or entry <= 0:
            return None
        tier = self._tier_for(pos)
        stop_pct = float(t.stop_loss_pct)
        if tier is not None and tier.get("stop_loss_pct") is not None:
            stop_pct = float(tier["stop_loss_pct"])
        stop = (
            entry * (1 - stop_pct / 100.0)
            if stop_pct > 0 else None
        )
        # the adaptive stepped ratchet (or the position's own
        # pinned trail) - shared with the positions payload so
        # the dashboard shows the same distance
        trail_pct = adaptive_trail_pct(t, pos, peak, entry)
        if trail_pct is not None and trail_pct > 0 and peak and peak > entry:
            trail = peak * (1 - trail_pct / 100.0)
            if stop is None or trail > stop:
                stop = trail
        # the atr floor (volatility-adaptive, default off) - the
        # tighter of the two wins
        atr = atr_stop_price(self._provider(), pos, peak, t)
        if atr is not None and (stop is None or atr > stop):
            return atr
        return stop

    def _provider(self):
        """The active quote provider object (the atr bars and the
        delta spots come from it, not from the per-position quote
        fn) - the module global the app registered at startup."""
        if self.provider is not None:
            return self.provider
        try:
            from consumer.trading.quotes import ACTIVE_QUOTE_PROVIDER

            return ACTIVE_QUOTE_PROVIDER
        except Exception:
            return None

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
        if expiry != et_now().date().isoformat():
            return False   # not an expiry-day position (ET clock)
        if not peak or peak <= entry:
            return False   # never had a gain to protect
        tier = self._tier_for(pos)
        if tier is not None and tier.get("back_to_entry") is False:
            return False
        return bid <= entry

    def check_once(self):
        self.last_run = time.time()
        t = self.cfg.trading
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
            # stop_price returns None when nothing is configured
            # for this position (no global/tier stop and no
            # per-position trailing) - the tp and b2e checks run
            # regardless
            stop = self.stop_price(entry, peak, pos)
            if stop is not None and bid <= stop:
                self._fire(pos, bid, reason="stop")
                continue
            # back to entry: a 0dte position that had a gain and
            # gave it all back is sold before it expires worthless
            if self._back_to_entry_hit(pos, entry, peak, bid):
                self._fire(pos, bid, reason="back_to_entry")
                continue
            # per-position take-profit: sell the rest when the
            # gain vs the entry premium reaches the position's
            # own target (set beside the position in the ui -
            # the ALL OUT alert is not guaranteed to arrive)
            tp = pos.get("tp_gain_pct")
            if tp and entry > 0 and bid >= entry * (1 + float(tp) / 100.0):
                self._fire(pos, bid, reason="tp")
        self._delta_warning()

    def _delta_warning(self):
        """The soft delta limit (rollout step 1): the book's net
        delta vs delta_cap_pct of equity, logged + discorded -
        never a rejection. Hard-gate later once the cap is
        calibrated against the dashboard's live delta. Rate
        limited to one notice per 10 minutes."""
        t = self.cfg.trading
        cap_pct = float(getattr(t, "delta_cap_pct", 0) or 0)
        if cap_pct <= 0:
            return
        provider = self._provider()
        if provider is None or not hasattr(provider, "stock_quote"):
            return
        rows = [
            p for p in self.store.list_positions(self.trader.mode)
            if p.get("right")
        ]
        if not rows:
            return
        spots = {}
        for u in sorted({p["underlying"] for p in rows}):
            spot = provider.stock_quote(u)
            if not spot and hasattr(provider, "index_quote"):
                try:
                    spot = provider.index_quote(u)
                except Exception:
                    spot = None
            if spot:
                spots[u] = spot
        from consumer.trading.greeks import portfolio_greeks

        # per-contract delta: the opend snapshot's own greek when
        # the build provides it, the flat-iv bs estimate otherwise
        # (the fallback is tagged in the discord payload)
        deltas = {}
        bs_needed = []
        for p in rows:
            d = None
            if hasattr(provider, "option_delta"):
                try:
                    d = provider.option_delta(p)
                except Exception:
                    d = None
            if d is None:
                bs_needed.append(p)
            else:
                deltas[p["contract_key"]] = d * 100 * int(
                    p.get("qty") or 0
                )
        agg = portfolio_greeks(bs_needed, spots)
        agg["delta"] = round(
            agg["delta"] + sum(deltas.values()), 2
        )
        equity = 0.0
        try:
            equity = sum(
                v for v in (self.trader.account.values() or {}).values()
                if v
            )
        except Exception:
            return
        if equity <= 0:
            return
        projected = abs(agg["delta"])
        limit = equity * cap_pct / 100.0
        now = time.time()
        if projected <= limit or now - self._delta_warn_ts < 600:
            return
        self._delta_warn_ts = now
        msg = (
            f"options delta warning: net options delta "
            f"{agg['delta']:+,.0f} "
            f"({projected / equity * 100:.1f}% of equity) past the "
            f"{cap_pct:g}% cap"
            + (
                f" - {agg['unpriced']} position(s) unpriced"
                if agg["unpriced"] else ""
            )
        )
        print(f"stop monitor: {msg}")
        notify_discord(
            self.webhook_url, "Delta limit (soft)",
            {"options_delta": agg["delta"], "equity": round(equity, 2),
             "cap_pct": cap_pct},
            ok=False,
        )

    def _fire(self, pos, bid, reason="stop"):
        tag = {"stop": "[STOP]", "back_to_entry": "[B2E]",
               "tp": "[TP]"}.get(reason, "[STOP]")
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
            stop_exit=True,
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
