from dataclasses import dataclass, field
from typing import Optional

from .account import account_label, effective_accounts


@dataclass
class ExecutionResult:
    ok: bool
    detail: str
    qty: int = 0
    price: Optional[float] = None
    order_id: Optional[str] = None
    breakdown: dict = field(default_factory=dict)


def effective_risk_pct(acct, cfg) -> float:
    if acct and acct.risk_per_trade_pct is not None:
        return float(acct.risk_per_trade_pct)
    return cfg.trading.risk_per_trade_pct


def effective_contract_cap(acct, cfg) -> int:
    if acct and acct.max_contracts_per_trade is not None:
        return int(acct.max_contracts_per_trade)
    return cfg.trading.max_contracts_per_trade


def contracts_for(alert, cfg, account_value: float, price, acct=None) -> int:
    if not price or price <= 0 or not account_value or account_value <= 0:
        return 0
    multiplier = 1.0
    if alert.size:
        multiplier = (cfg.trading.size_risk_multiplier or {}).get(alert.size, 1.0)
    budget = account_value * (effective_risk_pct(acct, cfg) / 100.0) * multiplier
    cost_per_contract = float(price) * 100
    qty = int(budget // cost_per_contract)
    cap = effective_contract_cap(acct, cfg)
    if cap and cap > 0:
        qty = min(qty, cap)
    return max(0, qty)


def account_sizing(alert, cfg, account) -> list:
    price = alert.premium if alert.kind == "option" else (
        alert.entry or alert.premium
    )
    rows = []
    for acct in effective_accounts(cfg):
        label = account_label(acct)
        value = None
        if account is not None:
            try:
                value = account.value(label)
            except Exception:
                value = None
        risk_pct = effective_risk_pct(acct, cfg)
        budget = value * (risk_pct / 100.0) if value else None
        contracts = None
        if budget is not None and price:
            multiplier = 1.0
            if alert.size:
                multiplier = (cfg.trading.size_risk_multiplier or {}).get(
                    alert.size, 1.0
                )
            contracts = int(
                (budget * multiplier) // (float(price) * 100)
            )
            cap = effective_contract_cap(acct, cfg)
            if cap and cap > 0:
                contracts = min(contracts, cap)
        rows.append(
            {
                "label": label,
                "value": value,
                "risk_pct": risk_pct,
                "budget": budget,
                "price": price,
                "contracts": contracts,
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


class PaperExecutor:
    mode = "paper"

    def __init__(self, cfg, store, account):
        self.cfg = cfg
        self.store = store
        self.account = account

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
                qty = contracts_for(alert, cfg, value, price, acct)
                if qty < 1:
                    breakdown[label] = "0 (risk budget too small)"
                    continue
                if _at_open_risk_cap(store, self.mode, label, value, cfg):
                    breakdown[label] = "skipped (open risk cap reached)"
                    continue
                store.apply_position(
                    self.mode, alert, qty, premium=price, account=label
                )
                store.adjust_paper_equity(-qty * price * 100, label)
                breakdown[label] = f"{qty}x @ {price}"
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
            store.apply_position(self.mode, alert, -qty, account=label)
            if alert.premium:
                store.adjust_paper_equity(qty * alert.premium * 100, label)
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
            for acct in effective_accounts(cfg):
                label = account_label(acct)
                qty = max(0, int(cfg.trading.position_size_cad / price))
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
            store.apply_position(self.mode, alert, -qty, account=label)
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

            self._ws = WealthsimpleV2()
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
                strike = float(opt["strikePrice"]["amount"])
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
                from .account import resolve_account_id

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
                qty = contracts_for(alert, cfg, value, limit, acct)
                if qty < 1:
                    breakdown[label] = "0 (risk budget too small)"
                    continue
                if _at_open_risk_cap(store, self.mode, label, value, cfg):
                    breakdown[label] = "skipped (open risk cap reached)"
                    continue
                order = ws.buy_option(
                    account_id, opt["id"], qty, float(limit)
                )
                store.apply_position(
                    self.mode, alert, qty, premium=float(limit), account=label
                )
                breakdown[label] = f"{qty}x @ {limit}"
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
                store.apply_position(self.mode, alert, -qty, account=label)
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

        for label, account_id, acct in self._account_ids(ws):
            if alert.action == "BUY":
                if not price:
                    breakdown[label] = "no quote price available"
                    continue
                qty = int(cfg.trading.position_size_cad / price)
                if qty < 1:
                    breakdown[label] = "0 (position size too small)"
                    continue
                if cfg.trading.order_type == "limit":
                    order = ws.limit_buy(
                        account_id, sec_id, qty, self._limit_price(price, "BUY")
                    )
                else:
                    order = ws.market_buy(account_id, sec_id, qty)
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
