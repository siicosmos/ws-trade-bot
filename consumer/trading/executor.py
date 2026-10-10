"""Order placement for both execution paths.

PaperExecutor books against the local ledger; WealthsimpleExecutor
places live orders with the WS client (resolve -> size -> gate ->
order -> pending row -> estimated booking). The sizing math lives
in sizing.py, the store-backed risk gates in risk_gates.py - this
module is the order path plus the ExecutionResult both paths
return.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional

from consumer.trading.risk import RiskEngine
from consumer.ws.account import account_label, effective_accounts

# the sizing + gate layers (re-exported: the pipeline, the
# dashboard and the tests import them from here)
from consumer.trading.sizing import (  # noqa: F401
    account_sizing, effective_contract_cap, effective_risk_pct,
    sell_quantity, sizing_multiplier, tier_for, tier_plan,
)
from consumer.trading.risk_gates import (  # noqa: F401
    at_cluster_cap, at_open_risk_cap, at_zero_dte_cap,
    effective_open_risk_cap, ledger_quote_price, lotto_gain_cap,
    position_row, stop_floor,
)


@dataclass
class ExecutionResult:
    ok: bool
    detail: str
    qty: int = 0
    price: Optional[float] = None
    order_id: Optional[str] = None
    breakdown: dict = field(default_factory=dict)


class PaperExecutor:
    mode = "paper"

    def __init__(self, cfg, store, account):
        self.cfg = cfg
        self.store = store
        self.account = account
        # one trade at a time - the stop monitor thread, the feed
        # thread and the flask request threads all execute here
        # (the live executor documents the same hazard)
        self._order_lock = threading.Lock()

    def _account_fx(self):
        """the account's usd->cad rate for cad-normalizing
        realized pnl (the ws account and the paper ledger both
        expose fx()); 1.0 when unavailable."""
        fn = getattr(self.account, "fx", None)
        try:
            return float(fn()) if callable(fn) else 1.0
        except Exception:
            return 1.0

    def _fx(self):
        # seeded ledgers are CAD; option premiums quote USD
        return self._account_fx()

    def _stock_is_usd(self, key):
        """Stock alerts quote in the listing's currency - the
        alerted names are US-listed (the option path assumes the
        same). A live quote for the symbol carries the security's
        currency; default to usd like the option path."""
        quotes_fn = getattr(self.account, "_quotes", None)
        if callable(quotes_fn):
            try:
                quote = quotes_fn().get(key)
                if quote is not None:
                    return bool(quote.get("usd", True))
            except Exception:
                pass
        return True

    def execute(self, alert, cfg, store) -> ExecutionResult:
        with self._order_lock:
            if not alert.stop_exit:
                if alert.ticker in (cfg.trading.skip_underlyings or []):
                    return ExecutionResult(
                        False,
                        f"{alert.ticker} in skip_underlyings",
                    )
                allowed, reason = RiskEngine(
                    cfg, store, None
                ).evaluate(alert)
                if not allowed:
                    return ExecutionResult(
                        False, f"risk gate: {reason}"
                    )
                if not store.claim_execution(
                    self.mode, alert.dedupe_key()
                ):
                    return ExecutionResult(
                        False,
                        "risk gate: duplicate execution in flight",
                    )
            if alert.kind == "option":
                return self._option(alert, cfg, store)
            return self._stock(alert, cfg, store)

    def _option(self, alert, cfg, store) -> ExecutionResult:
        key = alert.contract_key()
        if alert.action == "BUY":
            price = alert.premium
            if not price:
                return ExecutionResult(
                    False, f"[PAPER] {key} alert has no premium to size against"
                )
            breakdown = {}
            total = 0
            for acct in effective_accounts(cfg):
                label = account_label(acct)
                value = self.account.value(label)
                plan = tier_plan(alert, cfg, value, price, acct)
                # lotto / profits-only: the buy may spend at most a
                # fraction of today's realized sell gains
                gain_cap = lotto_gain_cap(store, self.mode, cfg, alert)
                if gain_cap is not None:
                    lotto_affordable = int(
                        gain_cap // (price * 100)
                    ) if price and price > 0 else 0
                    if lotto_affordable < max(1, plan["qty"]):
                        breakdown[label] = (
                            f"skipped (lotto budget ${gain_cap:,.2f} = "
                            f"{getattr(cfg.trading, 'lotto_gain_budget_pct', 75)}% "
                            f"of today's realized gain "
                            f"${store.realized_today(self.mode):,.2f})"
                        )
                        continue
                    qty = min(plan["qty"], lotto_affordable)
                else:
                    qty = plan["qty"]
                # the dynamic sizing scalar (vix / kelly, default
                # off) shrinks the tier quantity - never amplifies,
                # floored at 1 contract
                if qty >= 1:
                    scaled = sizing_multiplier(cfg, store, self.mode)
                    if scaled < 1.0:
                        qty = max(1, int(round(qty * scaled)))
                if qty < 1:
                    if plan["affordable"] >= 1:
                        breakdown[label] = (
                            f"0 (budget ${plan['budget']:,.0f} affords "
                            f"{plan['affordable']}, tier minimum "
                            f"{plan['tier_min'] or 1})"
                        )
                    else:
                        breakdown[label] = (
                            f"0 (budget ${plan['budget']:,.0f} < "
                            f"${plan['cost']:,.0f}/contract)"
                        )
                    continue
                note = ""
                if plan["affordable"] > qty:
                    if (
                        plan["tier_max"] is not None
                        and plan["affordable"] > plan["tier_max"]
                    ):
                        note = (
                            f" (tier max {plan['tier_max']}, "
                            f"could afford {plan['affordable']})"
                        )
                    else:
                        note = f" (capped from {plan['affordable']})"
                if at_open_risk_cap(store, self.mode, label, value, cfg, acct):
                    breakdown[label] = (
                        f"skipped (open risk cap reached, wanted {qty}x)"
                    )
                    continue
                if at_cluster_cap(
                    store, self.mode, label, value, cfg, alert, price
                ):
                    breakdown[label] = (
                        f"skipped (cluster cap reached: {alert.underlying} "
                        f"{alert.expiry} {alert.right} already "
                        f"{getattr(cfg.trading, 'cluster_cap_pct', 0):g}% "
                        f"of the account at risk)"
                    )
                    continue
                if at_zero_dte_cap(
                    store, self.mode, label, value, cfg, alert, price, qty
                ):
                    breakdown[label] = (
                        f"skipped (0dte daily cap reached: "
                        f"{alert.underlying} {alert.expiry} would take "
                        f"today's expiring risk past "
                        f"{getattr(cfg.trading, 'zero_dte_cap_pct', 0):g}% "
                        f"of the account)"
                    )
                    continue
                store.apply_position(
                    self.mode, alert, qty, premium=price, account=label
                )
                store.adjust_paper_equity(
                    -qty * price * 100 * self._fx(), label
                )
                breakdown[label] = f"{qty}x @ {price}{note}"
                total += qty
            ok = total > 0
            detail = (
                f"[PAPER] BUY {total}x {key} @ {price} | "
                + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
            )
            return ExecutionResult(ok, detail, qty=total, price=price,
                                  breakdown=breakdown)

        breakdown = {}
        total = 0
        # a sell with no usable price must never book blind: the
        # ..95-class typo executed with premium=None - the ledger
        # lost a contract with no cash and no realized. the
        # adaptive trail (already riding every position) takes
        # over instead: it sells on the fall from the peak, and
        # the stop floor sells marketable when it collapses
        if not alert.premium:
            price_now = ledger_quote_price(self.account, key)
            trail_mode = (
                getattr(cfg.trading, "unparsed_sell_action", "trail")
                == "trail"
            )
            for acct in effective_accounts(cfg):
                label = account_label(acct)
                held = store.get_position(self.mode, key, label)
                if held < 1:
                    breakdown[label] = "no position"
                    continue
                if trail_mode:
                    pos = position_row(store, self.mode, label, key)
                    peak = (
                        float(pos.get("peak_bid") or 0) if pos else 0.0
                    )
                    entry = (
                        float(pos.get("avg_premium") or 0) if pos
                        else 0.0
                    )
                    floor = stop_floor(cfg.trading, entry, pos)
                    breakdown[label] = (
                        f"price unparseable - trailing stop active "
                        f"(now ~{price_now or '?'}, peak {peak or '?'}, "
                        f"floor {floor or '?'})"
                    )
                else:
                    breakdown[label] = (
                        f"price unparseable - skipped "
                        f"(unparsed_sell_action: skip)"
                    )
            detail = (
                f"[PAPER] SELL {key} skipped - price unparseable; "
                f"the trailing stop takes it | "
                + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
            )
            return ExecutionResult(
                False, detail, qty=0, price=price_now,
                breakdown=breakdown,
            )
        for acct in effective_accounts(cfg):
            label = account_label(acct)
            held = store.get_position(self.mode, key, label)
            if held < 1:
                breakdown[label] = "no position"
                continue
            qty = sell_quantity(held, alert.scale)
            store.apply_position(
                    self.mode, alert, -qty,
                    premium=alert.premium, account=label,
                    fx=self._fx(),
                )
            if alert.premium:
                store.adjust_paper_equity(
                    qty * alert.premium * 100 * self._fx(), label
                )
            breakdown[label] = f"{qty}/{held}x @ {alert.premium}"
            total += qty
        ok = total > 0
        detail = (
            f"[PAPER] SELL {total}x {key} @ {alert.premium} | "
            + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
        )
        return ExecutionResult(ok, detail, qty=total, price=alert.premium,
                              breakdown=breakdown)

    def _stock(self, alert, cfg, store) -> ExecutionResult:
        key = alert.ticker
        if alert.action == "BUY":
            price = alert.entry
            if not price:
                return ExecutionResult(
                    False, f"[PAPER] {key} alert has no price to size against"
                )
            breakdown = {}
            total = 0
            # stock buys size as a percent of the account value
            # per tier (separate from the option contract
            # tiers); unsized alerts default to the medium tier
            stock_tiers = getattr(
                cfg.trading, "stock_size_tiers", None
            ) or {}
            tier_name = (
                (alert.size or "").lower() if alert.size else "medium"
            )
            for acct in effective_accounts(cfg):
                label = account_label(acct)
                pct = stock_tiers.get(
                    tier_name, stock_tiers.get("medium")
                )
                usd = self._stock_is_usd(key)
                fx = self._fx() if usd else 1.0
                if pct is not None:
                    value = self.account.value(label)
                    budget = max(0.0, float(value or 0) * pct / 100.0)
                    # the budget is cad - a usd listing spends it
                    # at the live fx
                    spendable = budget / fx if fx else budget
                else:
                    # the cad budget spends at the live fx too
                    spendable = (
                        float(cfg.trading.position_size_cad) / fx
                        if fx else float(cfg.trading.position_size_cad)
                    )
                qty = max(0, int(spendable / price))
                if qty < 1:
                    breakdown[label] = "0 (position size too small)"
                    continue
                if at_open_risk_cap(
                    store, self.mode, label, self.account.value(label),
                    cfg, acct,
                ):
                    breakdown[label] = "skipped (open risk cap reached)"
                    continue
                store.apply_position(
                    self.mode, alert, qty, premium=price, account=label
                )
                store.adjust_paper_equity(-qty * price * fx, label)
                breakdown[label] = f"{qty} @ {price}"
                total += qty
            ok = total > 0
            detail = (
                f"[PAPER] BUY {total} {key} @ {price} | "
                + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
            )
            return ExecutionResult(ok, detail, qty=total, price=price,
                                  breakdown=breakdown)

        breakdown = {}
        total = 0
        for acct in effective_accounts(cfg):
            label = account_label(acct)
            held = store.get_position(self.mode, key, label)
            if held < 1:
                breakdown[label] = "no position"
                continue
            qty = sell_quantity(held, alert.scale)
            # generic stock alerts carry the price in entry (the
            # service form sets both) - realized needs either
            store.apply_position(
                    self.mode, alert, -qty,
                    premium=alert.premium or alert.entry, account=label,
                    fx=self._fx() if self._stock_is_usd(key) else 1.0,
                )
            if alert.entry:
                store.adjust_paper_equity(
                    qty * alert.entry * (self._fx()
                                         if self._stock_is_usd(key)
                                         else 1.0), label
                )
            breakdown[label] = f"{qty}/{held} @ {alert.entry}"
            total += qty
        ok = total > 0
        detail = (
            f"[PAPER] SELL {total} {key} @ {alert.entry} | "
            + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
        )
        return ExecutionResult(ok, detail, qty=total, price=alert.entry,
                              breakdown=breakdown)


class WealthsimpleExecutor:
    mode = "live"

    def __init__(self, cfg, account):
        self.cfg = cfg
        self.account = account
        self._ws = None
        # one order path at a time: the stop monitor, the feed
        # client, flask request threads and the mirror all call
        # execute() concurrently - get_position -> ws order ->
        # apply_position is not atomic, and interleaved triggers
        # would double-sell the same live position
        self._order_lock = threading.Lock()

    def _client(self):
        if self._ws is None:
            from wealthsimple_python import WealthsimpleV2

            from consumer.ws.ws_http import install

            install()
            self._ws = WealthsimpleV2()
        from consumer.ws.ws_tokens import persist_env_tokens

        persist_env_tokens()
        return self._ws

    def _resolve_security(self, ws, ticker):
        hint = self.cfg.wealthsimple.exchange_hint or None
        sec_id = ws.get_ticker_id(ticker, hint)
        if sec_id:
            return sec_id
        results = ws.search_securities(ticker, security_group_ids=["stock", "adr"])
        for sec in results:
            if sec["stock"]["symbol"].upper() == ticker.upper():
                return sec["id"]
        if results:
            return results[0]["id"]
        return None

    def _resolve_option(self, ws, sec_id, alert):
        expiries = ws.get_option_expiry_dates(sec_id) or []
        chosen = None
        for exp in expiries:
            if not isinstance(exp, str):
                continue
            # exact match, or a year-less chain entry ("MM-DD")
            # matching the alert's month-day - a full-date chain
            # entry must match the year too (the old bare-suffix
            # match resolved the wrong contract across years)
            if exp == alert.expiry or (
                len(exp) == 5 and exp == alert.expiry[5:]
            ):
                chosen = exp
                break
        if chosen is None:
            return None, f"expiry {alert.expiry} not found for {alert.underlying}"

        opt_type = "CALL" if alert.right == "C" else "PUT"
        chain = ws.get_option_chain(sec_id, chosen, opt_type) or []
        for opt in chain:
            try:
                # the chain nests the strike under optionDetails
                # as a plain string ("102") - older shapes wrapped
                # it in {amount} or left it top-level
                od = opt.get("optionDetails") or {}
                strike = od.get(
                    "strikePrice", opt.get("strikePrice")
                )
                if isinstance(strike, dict):
                    strike = strike.get("amount")
                strike = float(strike)
            except (KeyError, TypeError, ValueError):
                continue
            if abs(strike - (alert.strike or 0)) < 0.001:
                return opt, None
        return None, f"strike {alert.strike} {opt_type} not found for {alert.expiry}"

    def _account_ids(self, ws):
        out = []
        seen_ids = set()
        fallback_used = False
        for acct in effective_accounts(self.cfg):
            account_id = acct.account_id
            if not account_id:
                # the id-less fallback resolves to ONE real account
                # (the first enabled one) - handing it to every
                # id-less row would place the same order twice on
                # the same account, so only the first row inherits
                # it and the rest are skipped
                if fallback_used:
                    continue
                from consumer.ws.account import resolve_account_id

                account_id = resolve_account_id(ws, self.cfg)
                fallback_used = True
            if account_id in seen_ids:
                # two rows resolving to the same real account
                # (explicit duplicate or a colliding fallback)
                # would double-trade it
                continue
            seen_ids.add(account_id)
            out.append((account_label(acct), account_id, acct))
        return out

    def _held_quantity(self, ws, account_id, ticker):
        positions = ws.get_positions(account_ids=[account_id]) or []
        for pos in positions:
            symbol = pos["security"]["stock"]["symbol"].upper()
            if symbol == ticker.upper():
                return int(pos["quantity"])
        return 0

    def _limit_price(self, base, side):
        offset = self.cfg.trading.limit_offset_pct / 100.0
        if side == "BUY":
            return round(base * (1 + offset), 2)
        return round(base * (1 - offset), 2)

    def _clamped_limit(self, base, premium, side, cfg,
                       marketable=False):
        """bound the live option limit to the alert's quoted
        premium: a stale or garbage chain quote must not be chased
        at any price (max_slippage_pct is the tolerated deviation;
        the post-fill slippage notice still reports the real fill
        against the estimate). a capped limit may simply not fill -
        that is the safe outcome. stop exits pass marketable=True:
        their sell limit never sits above the live bid (a limit
        above a gap-down market would not fill and the position
        would sit unprotected until the pending sweep reverses
        it)."""
        if not base or not premium:
            return base
        slip = getattr(cfg.trading, "max_slippage_pct", 2) / 100.0
        if side == "BUY":
            return round(min(base, premium * (1 + slip)), 2)
        if marketable:
            return round(min(base, premium * (1 - slip)), 2)
        return round(max(base, premium * (1 - slip)), 2)

    def execute(self, alert, cfg, store) -> ExecutionResult:
        # serialize the whole resolve-order-book path (see
        # _order_lock in __init__). the risk gates re-check under
        # the lock: the pipeline evaluated them in its own thread,
        # so two concurrent deliveries could both pass the caps
        # and then serialize only the order placement (the
        # account-dependent daily-loss gate ran pre-lock - the
        # store-backed gates are the racy ones)
        with self._order_lock:
            if not alert.stop_exit:
                if alert.ticker in (cfg.trading.skip_underlyings or []):
                    return ExecutionResult(
                        False,
                        f"{alert.ticker} in skip_underlyings "
                        f"(not tradeable on WS)",
                    )
                # auto-exits (stop loss / take profit / trailing)
                # bypass the gates by design - protection is never
                # dedupe-blocked or whitelist-blocked
                allowed, reason = RiskEngine(
                    cfg, store, None
                ).evaluate(alert)
                if not allowed:
                    return ExecutionResult(
                        False, f"risk gate: {reason}"
                    )
                # the trade row is recorded by the pipeline after
                # execute() returns - the claim closes that gap
                # for concurrent deliveries (the gates read the
                # trades table, which does not know about an
                # in-flight execution yet)
                if not store.claim_execution(
                    self.mode, alert.dedupe_key()
                ):
                    return ExecutionResult(
                        False,
                        "risk gate: duplicate execution in flight",
                    )
            if alert.kind == "option":
                return self._execute_option(alert, cfg, store)
            return self._execute_stock(alert, cfg, store)

    def _account_fx(self):
        """the account's usd->cad rate for cad-normalizing
        realized pnl (the ws account exposes fx()); 1.0 when
        unavailable."""
        fn = getattr(self.account, "fx", None)
        try:
            return float(fn()) if callable(fn) else 1.0
        except Exception:
            return 1.0

    def _execute_option(self, alert, cfg, store) -> ExecutionResult:
        ws = self._client()
        sec_id = self._resolve_security(ws, alert.underlying)
        if not sec_id:
            return ExecutionResult(
                False, f"could not resolve security id for {alert.underlying}"
            )

        opt, err = self._resolve_option(ws, sec_id, alert)
        if opt is None:
            return ExecutionResult(False, err)

        quote = opt.get("quoteV2") or opt.get("quote") or {}
        key = alert.contract_key()
        breakdown = {}
        total = 0
        order_ids = []
        first_error = None

        # a sell with no usable price never books blind (the
        # paper path documents the ..95 incident): the adaptive
        # trail already riding the position takes it instead -
        # it sells on the fall from the peak, and the stop
        # floor sells marketable when it collapses
        if alert.action == "SELL" and not alert.premium:
            bid = quote.get("bid")
            trail_mode = (
                getattr(cfg.trading, "unparsed_sell_action", "trail")
                == "trail"
            )
            for label, account_id, acct in self._account_ids(ws):
                held = store.get_position(self.mode, key, label)
                if held < 1:
                    breakdown[label] = "no position"
                    continue
                if trail_mode:
                    pos = position_row(store, self.mode, label, key)
                    entry = (
                        float(pos.get("avg_premium") or 0) if pos else 0
                    )
                    floor = stop_floor(cfg.trading, entry, pos)
                    breakdown[label] = (
                        f"price unparseable - trailing stop active "
                        f"(bid ~{bid or '?'}, floor {floor or '?'})"
                    )
                else:
                    breakdown[label] = (
                        f"price unparseable - skipped "
                        f"(unparsed_sell_action: skip)"
                    )
            detail = (
                f"[LIVE] SELL {key} skipped - price unparseable; "
                f"the trailing stop takes it | "
                + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
            )
            return ExecutionResult(
                False, detail, qty=0, price=bid,
                breakdown=breakdown,
            )

        for label, account_id, acct in self._account_ids(ws):
            try:
                if alert.action == "BUY":
                    limit = self._clamped_limit(
                        quote.get("ask") or alert.premium, alert.premium,
                        "BUY", cfg,
                    )
                    if not limit:
                        breakdown[label] = "no ask/premium to price order"
                        continue
                    try:
                        value = self.account.value(label)
                    except Exception:
                        value = None
                    plan = tier_plan(alert, cfg, value, limit, acct)
                    # lotto / profits-only: the buy may spend at most a
                    # fraction of today's realized sell gains
                    gain_cap = lotto_gain_cap(store, self.mode, cfg, alert)
                    if gain_cap is not None:
                        lotto_affordable = int(
                            gain_cap // (limit * 100)
                        ) if limit and limit > 0 else 0
                        if lotto_affordable < max(1, plan["qty"]):
                            breakdown[label] = (
                                f"skipped (lotto budget ${gain_cap:,.2f} = "
                                f"{getattr(cfg.trading, 'lotto_gain_budget_pct', 75)}% "
                                f"of today's realized gain "
                                f"${store.realized_today(self.mode):,.2f})"
                            )
                            continue
                        qty = min(plan["qty"], lotto_affordable)
                    else:
                        qty = plan["qty"]
                    # the dynamic sizing scalar (vix / kelly,
                    # default off) shrinks the tier quantity -
                    # never amplifies, floored at 1 contract
                    if qty >= 1:
                        scaled = sizing_multiplier(
                            cfg, store, self.mode
                        )
                        if scaled < 1.0:
                            qty = max(
                                1, int(round(qty * scaled))
                            )
                    if qty < 1:
                        if plan["affordable"] >= 1:
                            breakdown[label] = (
                                f"0 (budget ${plan['budget']:,.0f} affords "
                                f"{plan['affordable']}, tier minimum "
                                f"{plan['tier_min'] or 1})"
                            )
                        else:
                            breakdown[label] = (
                                f"0 (budget ${plan['budget']:,.0f} < "
                                f"${plan['cost']:,.0f}/contract)"
                            )
                        continue
                    note = ""
                    if plan["affordable"] > qty:
                        if (
                            plan["tier_max"] is not None
                            and plan["affordable"] > plan["tier_max"]
                        ):
                            note = (
                                f" (tier max {plan['tier_max']}, "
                                f"could afford {plan['affordable']})"
                            )
                        else:
                            note = f" (capped from {plan['affordable']})"
                    if at_open_risk_cap(store, self.mode, label, value, cfg, acct):
                        breakdown[label] = (
                            f"skipped (open risk cap reached, wanted {qty}x)"
                        )
                        continue
                    if at_cluster_cap(
                        store, self.mode, label, value, cfg, alert, limit
                    ):
                        breakdown[label] = (
                            f"skipped (cluster cap reached: {alert.underlying} "
                            f"{alert.expiry} {alert.right} already "
                            f"{getattr(cfg.trading, 'cluster_cap_pct', 0):g}% "
                            f"of the account at risk)"
                        )
                        continue
                    if at_zero_dte_cap(
                        store, self.mode, label, value, cfg,
                        alert, limit, qty
                    ):
                        breakdown[label] = (
                            f"skipped (0dte daily cap reached: "
                            f"{alert.underlying} {alert.expiry} would take "
                            f"today's expiring risk past "
                            f"{getattr(cfg.trading, 'zero_dte_cap_pct', 0):g}% "
                            f"of the account)"
                        )
                        continue
                    order = ws.buy_option(
                        account_id, opt["id"], qty, float(limit)
                    )
                    pre_qty, pre_avg = store.position_state(
                        self.mode, label, key
                    )
                    # the pending row lands before the ledger
                    # booking: a booking failure then leaves a row
                    # the sweep can reconcile or reverse (instead
                    # of an orphaned live order)
                    store.record_pending_order(
                        self.mode, label, str(order.get("orderId") or ""),
                        "option", key, alert.underlying, alert.expiry,
                        alert.strike, alert.right, alert.action, qty,
                        float(limit), pre_qty=pre_qty, pre_avg=pre_avg,
                    )
                    # the estimated booking rides the ledger now (the
                    # gates depend on it); the mirror reconciles the
                    # actual fill or reverses the estimate
                    store.apply_position(
                        self.mode, alert, qty, premium=float(limit), account=label
                    )
                    breakdown[label] = f"{qty}x @ {limit}{note}"
                    total += qty
                    order_ids.append(str(order.get("orderId") or ""))
                else:
                    held = store.get_position(self.mode, key, label)
                    if held < 1:
                        breakdown[label] = "no position in ledger"
                        continue
                    qty = sell_quantity(held, alert.scale)
                    limit = self._clamped_limit(
                        quote.get("bid") or alert.premium, alert.premium,
                        "SELL", cfg, marketable=alert.stop_exit,
                    )
                    if not limit:
                        breakdown[label] = "no bid/premium to price order"
                        continue
                    order = ws.sell_option(
                        account_id, opt["id"], qty, float(limit)
                    )
                    pre_qty, pre_avg = store.position_state(
                        self.mode, label, key
                    )
                    # the pending row lands before the ledger
                    # booking (see the buy path)
                    store.record_pending_order(
                        self.mode, label, str(order.get("orderId") or ""),
                        "option", key, alert.underlying, alert.expiry,
                        alert.strike, alert.right, alert.action, qty,
                        float(limit), pre_qty=pre_qty, pre_avg=pre_avg,
                    )
                    store.apply_position(
                        self.mode, alert, -qty,
                        premium=float(limit), account=label,
                        fx=self._account_fx(),
                    )
                    breakdown[label] = f"{qty}/{held}x @ {limit}"
                    total += qty
                    order_ids.append(str(order.get("orderId") or ""))

            except Exception as e:
                # per-account fault isolation: account #1's order
                # is already live when account #2 raises - record
                # the failure and keep going (the pending row and
                # the ledger book inside this block, so a failure
                # there leaves that account untouched)
                breakdown[label] = f"error: {e}"
                first_error = first_error or e

        # nothing landed anywhere: raise so the pipeline records
        # an "error" row (a returned failure records "skipped",
        # which would mislabel a broker failure)
        if total == 0 and first_error is not None:
            raise first_error
        ok = total > 0
        action = alert.action
        detail = (
            f"{action} {total}x {key} | "
            + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
        )
        ref = (
            (quote.get("bid") if alert.action == "SELL"
             else quote.get("ask"))
            or quote.get("bid") or quote.get("ask") or alert.premium
        )
        return ExecutionResult(
            ok, detail, qty=total,
            price=ref,
            order_id=",".join(order_ids) or None,
            breakdown=breakdown,
        )

    def _execute_stock(self, alert, cfg, store) -> ExecutionResult:
        ws = self._client()
        sec_id = self._resolve_security(ws, alert.ticker)
        if not sec_id:
            return ExecutionResult(
                False, f"could not resolve security id for {alert.ticker}"
            )

        quote = ws.get_security_quote(sec_id)
        price = quote.get("ask") or quote.get("price")
        # the listing's currency sizes the cad budget: a usd
        # listing spends it at the live fx (the paper path does
        # the same via the ledger quotes)
        q_currency = str(
            (quote.get("quoteV2") or {}).get("currency")
            or quote.get("currency") or ""
        ).upper()
        stock_fx = (
            self._account_fx() if q_currency == "USD" else 1.0
        )
        breakdown = {}
        total = 0
        order_ids = []
        first_error = None

        # stock buys size as a percent of the account value per
        # tier - same as the paper path, separate from the
        # option contract tiers; unsized alerts default to the
        # medium tier
        stock_tiers = getattr(
            cfg.trading, "stock_size_tiers", None
        ) or {}
        tier_name = (
            (alert.size or "").lower() if alert.size else "medium"
        )

        for label, account_id, acct in self._account_ids(ws):
            try:
                if alert.action == "BUY":
                    if not price:
                        breakdown[label] = "no quote price available"
                        continue
                    pct = stock_tiers.get(
                        tier_name, stock_tiers.get("medium")
                    )
                    # fetched on both budget paths: the open-risk cap
                    # check below is skipped when value is None, so a
                    # missing tier must not disable the cap
                    value = self.account.value(label)
                    if pct is not None:
                        budget = max(
                            0.0, float(value or 0) * pct / 100.0
                        )
                    else:
                        budget = float(cfg.trading.position_size_cad)
                    # the cad budget spends at the listing's fx
                    qty = int(budget / stock_fx / price)
                    if qty < 1:
                        breakdown[label] = "0 (position size too small)"
                        continue
                    # the paper path checks this guard too: a live
                    # stock buy must respect the open-risk cap
                    if at_open_risk_cap(
                        store, self.mode, label, value, cfg, acct
                    ):
                        breakdown[label] = (
                            f"skipped (open risk cap reached, wanted {qty})"
                        )
                        continue
                    if cfg.trading.order_type == "limit":
                        order = ws.limit_buy(
                            account_id, sec_id, qty, self._limit_price(price, "BUY")
                        )
                    else:
                        order = ws.market_buy(account_id, sec_id, qty)
                    pre_qty, pre_avg = store.position_state(
                        self.mode, label, alert.ticker
                    )
                    store.record_pending_order(
                        self.mode, label, str(order.get("orderId") or ""),
                        "stock", alert.ticker, alert.ticker, None, None,
                        None, alert.action, qty, price,
                        pre_qty=pre_qty, pre_avg=pre_avg,
                    )
                    breakdown[label] = f"{qty} @ ~{price}"
                    total += qty
                    order_ids.append(str(order.get("orderId") or ""))
                else:
                    held = self._held_quantity(ws, account_id, alert.ticker)
                    if held < 1:
                        # no shorting: a sell alert for a name the
                        # account doesn't hold is skipped (the option
                        # path is ledger-gated the same way; deriving a
                        # quantity from position_size_cad would open an
                        # unintended short at market)
                        breakdown[label] = "no position to sell"
                        continue
                    if cfg.trading.order_type == "limit" and price:
                        order = ws.limit_sell(
                            account_id, sec_id, held, self._limit_price(price, "SELL")
                        )
                    else:
                        order = ws.market_sell(account_id, sec_id, held)
                    pre_qty, pre_avg = store.position_state(
                        self.mode, label, alert.ticker
                    )
                    store.record_pending_order(
                        self.mode, label, str(order.get("orderId") or ""),
                        "stock", alert.ticker, alert.ticker, None, None,
                        None, alert.action, held, price,
                        pre_qty=pre_qty, pre_avg=pre_avg,
                    )
                    breakdown[label] = f"{held} @ ~{price}" if price else f"{held}"
                    total += held
                    order_ids.append(str(order.get("orderId") or ""))

            except Exception as e:
                # per-account fault isolation (the option loop
                # documents the hazard): account #1's order is
                # already live when account #2 raises
                breakdown[label] = f"error: {e}"
                first_error = first_error or e
        # nothing landed anywhere: raise so the pipeline records
        # an "error" row (a returned failure records "skipped",
        # which would mislabel a broker failure)
        if total == 0 and first_error is not None:
            raise first_error
        ok = total > 0
        detail = (
            f"{alert.action} {total} {alert.ticker} | "
            + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
        )
        return ExecutionResult(
            ok, detail, qty=total, price=price,
            order_id=",".join(order_ids) or None,
            breakdown=breakdown,
        )
