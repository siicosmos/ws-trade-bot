from dataclasses import dataclass, field
from typing import Optional

from ..ws.account import account_label, effective_accounts


@dataclass
class ExecutionResult:
    ok: bool
    detail: str
    qty: int = 0
    price: Optional[float] = None
    order_id: Optional[str] = None
    breakdown: dict = field(default_factory=dict)


def tier_for(alert, cfg):
    if alert and alert.size:
        tier = (cfg.trading.size_tiers or {}).get(alert.size)
        if tier:
            return tier
    return None


def effective_risk_pct(acct, cfg, alert=None) -> float:
    tier = tier_for(alert, cfg)
    if tier is not None:
        return float(tier["risk_pct_max"])
    if acct and acct.risk_per_trade_pct is not None:
        return float(acct.risk_per_trade_pct)
    return cfg.trading.risk_per_trade_pct


def effective_contract_cap(acct, cfg) -> int:
    if acct and acct.max_contracts_per_trade is not None:
        return int(acct.max_contracts_per_trade)
    return cfg.trading.max_contracts_per_trade


def tier_plan(alert, cfg, account_value, price, acct=None) -> dict:
    cost = float(price) * 100 if price else 0.0
    tier = tier_for(alert, cfg)
    risk_pct = effective_risk_pct(acct, cfg, alert)
    if account_value and account_value > 0:
        budget = float(account_value) * risk_pct / 100.0
    else:
        budget = 0.0
    affordable = int(budget // cost) if cost > 0 and account_value else 0
    cap = effective_contract_cap(acct, cfg)

    tier_min = tier_max = None
    if tier is not None:
        tier_min = int(tier["contracts_min"])
        tier_max = int(tier["contracts_max"])
        qty = min(affordable, tier_max)
        if cap and cap > 0:
            qty = min(qty, cap)
        if qty < tier_min:
            qty = 0
    else:
        qty = affordable
        if cap and cap > 0:
            qty = min(qty, cap)

    return {
        "qty": max(0, qty),
        "affordable": affordable,
        "budget": budget,
        "risk_pct": risk_pct,
        "tier_min": tier_min,
        "tier_max": tier_max,
        "cap": cap,
        "cost": cost,
        "tier": alert.size,
    }



def account_sizing(alert, cfg, account, store=None) -> list:
    price = alert.premium if alert.kind == "option" else (
        alert.entry or alert.premium
    )
    mode = "live" if cfg.trading.mode == "live" else "paper"
    rows = []
    for acct in effective_accounts(cfg):
        label = account_label(acct)
        value = None
        if account is not None:
            try:
                value = account.value(label)
            except Exception:
                value = None

        plan = tier_plan(alert, cfg, value, price, acct)
        contracts = plan["qty"] if (value is not None and price) else None
        warnings = []

        stale_fn = getattr(account, "stale_age", None)
        if value is not None and callable(stale_fn):
            age = stale_fn(label)
            if age:
                warnings.append(
                    f"using cached account value ({age} old)"
                )

        if value is not None and value > 0 and price:
            if plan["qty"] < 1:
                if plan["affordable"] >= 1:
                    warnings.append(
                        f"{plan['risk_pct']:g}% budget "
                        f"${plan['budget']:,.2f} affords "
                        f"{plan['affordable']}, tier minimum is "
                        f"{plan['tier_min'] or 1}"
                    )
                else:
                    warnings.append(
                        f"{plan['risk_pct']:g}% budget "
                        f"${plan['budget']:,.2f} can't cover "
                        f"1 contract at ${plan['cost']:,.0f}"
                    )
            elif plan["affordable"] > plan["qty"]:
                if (
                    plan["cap"]
                    and plan["cap"] > 0
                    and (plan["tier_max"] is None or plan["cap"] < plan["tier_max"])
                ):
                    warnings.append(
                        f"capped at account max of {plan['cap']} "
                        f"(budget could afford {plan['affordable']})"
                    )
                elif (
                    plan["tier_max"] is not None
                    and plan["affordable"] > plan["tier_max"]
                ):
                    tier_name = plan.get("tier") or "size"
                    warnings.append(
                        f"capped at {tier_name} tier max of "
                        f"{plan['tier_max']} "
                        f"(budget could afford {plan['affordable']})"
                    )

        if (
            alert.action == "BUY"
            and store is not None
            and value
            and plan["qty"]
            and cfg.trading.max_open_risk_pct > 0
        ):
            open_risk = store.open_risk(mode, label)
            limit = value * (cfg.trading.max_open_risk_pct / 100.0)
            if open_risk >= limit:
                warnings.append(
                    f"open risk cap reached "
                    f"(${open_risk:,.0f} of ${limit:,.0f} deployed)"
                )

        rows.append(
            {
                "label": label,
                "value": value,
                "risk_pct": plan["risk_pct"],
                "budget": plan["budget"],
                "price": price,
                "contracts": contracts,
                "final_contracts": contracts,
                "actual_risk": (
                    plan["qty"] * plan["cost"]
                    if contracts is not None
                    else None
                ),
                "warnings": warnings,
            }
        )
    return rows


def sell_quantity(held: int, scale: Optional[float]) -> int:
    if held <= 0:
        return 0
    if scale is None or scale >= 1.0:
        return held
    qty = int(held * scale + 0.5)
    return min(held, max(1, qty))


def _at_open_risk_cap(store, mode, label, value, cfg) -> bool:
    if cfg.trading.max_open_risk_pct <= 0 or value is None or value <= 0:
        return False
    open_risk = store.open_risk(mode, label)
    limit = value * (cfg.trading.max_open_risk_pct / 100.0)
    return open_risk >= limit


def is_lotto_alert(alert) -> bool:
    """Lotto rules: an explicit lotto size, the "hero or zero"
    phrasing (parsed as lotto) or a profits-only qualifier."""
    return (
        bool(getattr(alert, "size", None) == "lotto")
        or bool(getattr(alert, "profits_only", False))
    )


def lotto_gain_cap(store, mode, cfg, alert) -> Optional[float]:
    """The max $ a lotto / profits-only buy may spend today: a
    fraction (default 75%) of what was realized selling today.
    Zero gains means zero lotto budget."""
    if alert.size != "lotto" and not getattr(
        alert, "profits_only", False
    ):
        return None
    frac = float(getattr(cfg.trading, "lotto_gain_budget_pct", 75.0))
    return max(0.0, store.realized_today(mode)) * frac / 100.0


class PaperExecutor:
    mode = "paper"

    def __init__(self, cfg, store, account):
        self.cfg = cfg
        self.store = store
        self.account = account

    def _fx(self):
        # seeded ledgers are CAD; option premiums quote USD
        fn = getattr(self.account, "fx", None)
        return float(fn()) if callable(fn) else 1.0

    def execute(self, alert, cfg, store) -> ExecutionResult:
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
                if _at_open_risk_cap(store, self.mode, label, value, cfg):
                    breakdown[label] = (
                        f"skipped (open risk cap reached, wanted {qty}x)"
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
        for acct in effective_accounts(cfg):
            label = account_label(acct)
            held = store.get_position(self.mode, key, label)
            if held < 1:
                breakdown[label] = "no position"
                continue
            qty = sell_quantity(held, alert.scale)
            store.apply_position(
                    self.mode, alert, -qty,
                    premium=alert.premium, account=label
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
                if pct is not None:
                    value = self.account.value(label)
                    budget = max(0.0, float(value or 0) * pct / 100.0)
                else:
                    budget = float(cfg.trading.position_size_cad)
                qty = max(0, int(budget / price))
                if qty < 1:
                    breakdown[label] = "0 (position size too small)"
                    continue
                if _at_open_risk_cap(
                    store, self.mode, label, self.account.value(label), cfg
                ):
                    breakdown[label] = "skipped (open risk cap reached)"
                    continue
                store.apply_position(
                    self.mode, alert, qty, premium=price, account=label
                )
                store.adjust_paper_equity(-qty * price, label)
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
                    premium=alert.premium or alert.entry, account=label
                )
            if alert.entry:
                store.adjust_paper_equity(qty * alert.entry, label)
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

    def _client(self):
        if self._ws is None:
            from wealthsimple_python import WealthsimpleV2

            from ..ws.ws_http import install

            install()
            self._ws = WealthsimpleV2()
        from ..ws.ws_tokens import persist_env_tokens

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
            if exp == alert.expiry or exp.endswith(alert.expiry[5:]):
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
        for acct in effective_accounts(self.cfg):
            account_id = acct.account_id
            if not account_id:
                from ..ws.account import resolve_account_id

                account_id = resolve_account_id(ws, self.cfg)
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

    def execute(self, alert, cfg, store) -> ExecutionResult:
        if alert.ticker in (cfg.trading.skip_underlyings or []):
            return ExecutionResult(
                False, f"{alert.ticker} in skip_underlyings (not tradeable on WS)"
            )

        if alert.kind == "option":
            return self._execute_option(alert, cfg, store)
        return self._execute_stock(alert, cfg, store)

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

        quote = opt.get("quote") or {}
        key = alert.contract_key()
        breakdown = {}
        total = 0
        order_ids = []

        for label, account_id, acct in self._account_ids(ws):
            if alert.action == "BUY":
                limit = quote.get("ask") or alert.premium
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
                if _at_open_risk_cap(store, self.mode, label, value, cfg):
                    breakdown[label] = (
                        f"skipped (open risk cap reached, wanted {qty}x)"
                    )
                    continue
                order = ws.buy_option(
                    account_id, opt["id"], qty, float(limit)
                )
                pre_qty, pre_avg = store.position_state(
                    self.mode, key, label
                )
                store.apply_position(
                    self.mode, alert, qty, premium=float(limit), account=label
                )
                # the estimated booking rides the ledger now (the
                # gates depend on it); the mirror reconciles the
                # actual fill or reverses the estimate
                store.record_pending_order(
                    self.mode, label, str(order.get("orderId") or ""),
                    "option", key, alert.underlying, alert.expiry,
                    alert.strike, alert.right, alert.action, qty,
                    float(limit), pre_qty=pre_qty, pre_avg=pre_avg,
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
                limit = quote.get("bid") or alert.premium
                if not limit:
                    breakdown[label] = "no bid/premium to price order"
                    continue
                order = ws.sell_option(
                    account_id, opt["id"], qty, float(limit)
                )
                pre_qty, pre_avg = store.position_state(
                    self.mode, key, label
                )
                store.apply_position(
                    self.mode, alert, -qty,
                    premium=float(limit), account=label
                )
                store.record_pending_order(
                    self.mode, label, str(order.get("orderId") or ""),
                    "option", key, alert.underlying, alert.expiry,
                    alert.strike, alert.right, alert.action, qty,
                    float(limit), pre_qty=pre_qty, pre_avg=pre_avg,
                )
                breakdown[label] = f"{qty}/{held}x @ {limit}"
                total += qty
                order_ids.append(str(order.get("orderId") or ""))

        ok = total > 0
        action = alert.action
        detail = (
            f"{action} {total}x {key} | "
            + "; ".join(f"{k}: {v}" for k, v in breakdown.items())
        )
        return ExecutionResult(
            ok, detail, qty=total,
            price=quote.get("ask") or quote.get("bid") or alert.premium,
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
        breakdown = {}
        total = 0
        order_ids = []

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
            if alert.action == "BUY":
                if not price:
                    breakdown[label] = "no quote price available"
                    continue
                pct = stock_tiers.get(
                    tier_name, stock_tiers.get("medium")
                )
                value = None
                if pct is not None:
                    value = self.account.value(label)
                    budget = max(
                        0.0, float(value or 0) * pct / 100.0
                    )
                else:
                    budget = float(cfg.trading.position_size_cad)
                qty = int(budget / price)
                if qty < 1:
                    breakdown[label] = "0 (position size too small)"
                    continue
                # the paper path checks this guard too: a live
                # stock buy must respect the open-risk cap
                if _at_open_risk_cap(
                    store, self.mode, label, value, cfg
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
                    self.mode, alert.ticker, label
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
                    if cfg.trading.sell_only_if_held:
                        breakdown[label] = "no position to sell"
                        continue
                    if not price:
                        breakdown[label] = "no quote price available"
                        continue
                    held = int(cfg.trading.position_size_cad / price)
                    if held < 1:
                        breakdown[label] = "computed sell quantity below 1"
                        continue
                if cfg.trading.order_type == "limit" and price:
                    order = ws.limit_sell(
                        account_id, sec_id, held, self._limit_price(price, "SELL")
                    )
                else:
                    order = ws.market_sell(account_id, sec_id, held)
                pre_qty, pre_avg = store.position_state(
                    self.mode, alert.ticker, label
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
