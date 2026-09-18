from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionResult:
    ok: bool
    detail: str
    qty: int = 0
    price: Optional[float] = None
    order_id: Optional[str] = None


def contracts_for(alert, cfg, account_value: float, price) -> int:
    if not price or price <= 0 or account_value <= 0:
        return 0
    multiplier = 1.0
    if alert.size:
        multiplier = (cfg.trading.size_risk_multiplier or {}).get(alert.size, 1.0)
    budget = account_value * (cfg.trading.risk_per_trade_pct / 100.0) * multiplier
    cost_per_contract = float(price) * 100
    qty = int(budget // cost_per_contract)
    cap = cfg.trading.max_contracts_per_trade
    if cap and cap > 0:
        qty = min(qty, cap)
    return max(0, qty)


def sell_quantity(held: int, scale: Optional[float]) -> int:
    if held <= 0:
        return 0
    if scale is None or scale >= 1.0:
        return held
    qty = round(held * scale)
    return min(held, max(1, qty))


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
            qty = contracts_for(alert, cfg, self.account.value(), price)
            if qty < 1:
                budget = self.account.value() * (
                    cfg.trading.risk_per_trade_pct / 100.0
                )
                return ExecutionResult(
                    False,
                    f"[PAPER] risk budget {budget:.0f} too small for {key} "
                    f"@ {price} (cost {price * 100:.0f}/contract)",
                )
            store.apply_position(self.mode, alert, qty, premium=price)
            store.adjust_paper_equity(-qty * price * 100)
            return ExecutionResult(
                True, f"[PAPER] BUY {qty}x {key} @ {price}", qty=qty, price=price
            )
        held = store.get_position(self.mode, key)
        if held < 1:
            return ExecutionResult(False, f"[PAPER] no {key} position to sell")
        qty = sell_quantity(held, alert.scale)
        store.apply_position(self.mode, alert, -qty)
        if alert.premium:
            store.adjust_paper_equity(qty * alert.premium * 100)
        return ExecutionResult(
            True,
            f"[PAPER] SELL {qty}/{held}x {key} @ {alert.premium}",
            qty=qty,
            price=alert.premium,
        )

    def _stock(self, alert, cfg, store) -> ExecutionResult:
        key = alert.ticker
        if alert.action == "BUY":
            price = alert.entry
            if not price:
                return ExecutionResult(
                    False, f"[PAPER] {key} alert has no price to size against"
                )
            qty = max(0, int(cfg.trading.position_size_cad / price))
            if qty < 1:
                return ExecutionResult(
                    False,
                    f"[PAPER] position size {cfg.trading.position_size_cad} "
                    f"too small for {key} @ {price}",
                )
            store.apply_position(self.mode, alert, qty, premium=price)
            store.adjust_paper_equity(-qty * price)
            return ExecutionResult(
                True, f"[PAPER] BUY {qty} {key} @ {price}", qty=qty, price=price
            )
        held = store.get_position(self.mode, key)
        if held < 1:
            return ExecutionResult(False, f"[PAPER] no {key} position to sell")
        qty = sell_quantity(held, alert.scale)
        store.apply_position(self.mode, alert, -qty)
        if alert.entry:
            store.adjust_paper_equity(qty * alert.entry)
        return ExecutionResult(
            True,
            f"[PAPER] SELL {qty}/{held} {key} @ {alert.entry}",
            qty=qty,
            price=alert.entry,
        )


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

    def _account_id(self, ws):
        from .account import resolve_account_id

        return resolve_account_id(ws, self.cfg)

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
        account_id = self._account_id(ws)
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

        if alert.action == "BUY":
            limit = quote.get("ask") or alert.premium
            if not limit:
                return ExecutionResult(False, "no ask/premium to price order")
            qty = contracts_for(alert, cfg, self.account.value(), limit)
            if qty < 1:
                budget = self.account.value() * (
                    cfg.trading.risk_per_trade_pct / 100.0
                )
                return ExecutionResult(
                    False,
                    f"risk budget {budget:.0f} too small for {key} "
                    f"@ {limit} (cost {float(limit) * 100:.0f}/contract)",
                )
            order = ws.buy_option(account_id, opt["id"], qty, float(limit))
            store.apply_position(self.mode, alert, qty, premium=float(limit))
            return ExecutionResult(
                True,
                f"BUY {qty}x {key} @ {limit}",
                qty=qty,
                price=float(limit),
                order_id=str(order.get("orderId") or ""),
            )

        held = store.get_position(self.mode, key)
        if held < 1:
            return ExecutionResult(False, f"no {key} position in ledger to sell")
        qty = sell_quantity(held, alert.scale)
        limit = quote.get("bid") or alert.premium
        if not limit:
            return ExecutionResult(False, "no bid/premium to price order")
        order = ws.sell_option(account_id, opt["id"], qty, float(limit))
        store.apply_position(self.mode, alert, -qty)
        return ExecutionResult(
            True,
            f"SELL {qty}/{held}x {key} @ {limit}",
            qty=qty,
            price=float(limit),
            order_id=str(order.get("orderId") or ""),
        )

    def _execute_stock(self, alert, cfg, store) -> ExecutionResult:
        ws = self._client()
        account_id = self._account_id(ws)
        sec_id = self._resolve_security(ws, alert.ticker)
        if not sec_id:
            return ExecutionResult(
                False, f"could not resolve security id for {alert.ticker}"
            )

        quote = ws.get_security_quote(sec_id)
        price = quote.get("ask") or quote.get("price")

        if alert.action == "BUY":
            if not price:
                return ExecutionResult(False, "no quote price available")
            qty = int(cfg.trading.position_size_cad / price)
            if qty < 1:
                return ExecutionResult(
                    False,
                    f"position size {cfg.trading.position_size_cad} too small "
                    f"for price {price}",
                )
            if cfg.trading.order_type == "limit":
                order = ws.limit_buy(
                    account_id, sec_id, qty, self._limit_price(price, "BUY")
                )
            else:
                order = ws.market_buy(account_id, sec_id, qty)
            result = ExecutionResult(
                True,
                f"BUY {qty} {alert.ticker} @ ~{price}",
                qty=qty,
                price=price,
                order_id=str(order.get("orderId") or ""),
            )
            if (
                cfg.trading.place_stop_loss
                and alert.stop_loss
                and cfg.trading.order_type != "market"
            ):
                ws.stop_limit_sell(
                    account_id,
                    sec_id,
                    qty,
                    self._limit_price(alert.stop_loss, "SELL"),
                    alert.stop_loss,
                )
            return result

        held = self._held_quantity(ws, account_id, alert.ticker)
        if held < 1:
            if cfg.trading.sell_only_if_held:
                return ExecutionResult(
                    False, f"no {alert.ticker} position to sell"
                )
            if not price:
                return ExecutionResult(False, "no quote price available")
            held = int(cfg.trading.position_size_cad / price)
            if held < 1:
                return ExecutionResult(False, "computed sell quantity below 1")

        if cfg.trading.order_type == "limit" and price:
            order = ws.limit_sell(
                account_id, sec_id, held, self._limit_price(price, "SELL")
            )
        else:
            order = ws.market_sell(account_id, sec_id, held)
        return ExecutionResult(
            True,
            f"SELL {held} {alert.ticker} @ ~{price}"
            if price
            else f"SELL {held} {alert.ticker}",
            qty=held,
            price=price,
            order_id=str(order.get("orderId") or ""),
        )
