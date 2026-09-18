from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionResult:
    ok: bool
    detail: str
    qty: int = 0
    price: Optional[float] = None
    order_id: Optional[str] = None


def contracts_for(alert, cfg) -> int:
    sizes = cfg.trading.contracts_by_size or {}
    if alert.size and alert.size in sizes:
        return int(sizes[alert.size])
    return int(cfg.trading.default_contracts)


def sell_quantity(held: int, scale: Optional[float]) -> int:
    if held <= 0:
        return 0
    if scale is None or scale >= 1.0:
        return held
    qty = round(held * scale)
    return min(held, max(1, qty))


class PaperExecutor:
    mode = "paper"

    def execute(self, alert, cfg, store) -> ExecutionResult:
        if alert.kind == "option":
            key = alert.contract_key()
            if alert.action == "BUY":
                qty = contracts_for(alert, cfg)
                store.apply_position(self.mode, alert, qty)
                price = alert.premium
                return ExecutionResult(
                    True,
                    f"[PAPER] BUY {qty}x {key} @ {price}",
                    qty=qty,
                    price=price,
                )
            held = store.get_position(self.mode, key)
            qty = sell_quantity(held, alert.scale)
            if held < 1:
                return ExecutionResult(
                    False, f"[PAPER] no {key} position to sell"
                )
            store.apply_position(self.mode, alert, -qty)
            return ExecutionResult(
                True,
                f"[PAPER] SELL {qty}/{held}x {key} @ {alert.premium}",
                qty=qty,
                price=alert.premium,
            )

        key = alert.ticker
        if alert.action == "BUY":
            price = alert.entry
            qty = 0
            if price:
                qty = max(0, int(cfg.trading.position_size_cad / price))
                store.apply_position(self.mode, alert, qty)
            return ExecutionResult(
                True,
                f"[PAPER] BUY {qty} {key} @ {price}",
                qty=qty,
                price=price,
            )
        held = store.get_position(self.mode, key)
        qty = sell_quantity(held, alert.scale)
        if held < 1:
            return ExecutionResult(
                False, f"[PAPER] no {key} position to sell"
            )
        store.apply_position(self.mode, alert, -qty)
        return ExecutionResult(
            True,
            f"[PAPER] SELL {qty}/{held} {key} @ {alert.entry}",
            qty=qty,
            price=alert.entry,
        )


class WealthsimpleExecutor:
    mode = "live"

    def __init__(self, cfg):
        self.cfg = cfg
        self._ws = None

    def _client(self):
        if self._ws is None:
            from wealthsimple_python import WealthsimpleV2

            self._ws = WealthsimpleV2()
        return self._ws

    def _account_id(self, ws):
        if self.cfg.wealthsimple.account_id:
            return self.cfg.wealthsimple.account_id
        accounts = ws.get_accounts()
        active = [a for a in accounts if a.get("status") == "ACTIVE"]
        pool = active or accounts
        return pool[0]["id"]

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
            qty = contracts_for(alert, cfg)
            limit = quote.get("ask") or alert.premium
            if not limit:
                return ExecutionResult(False, "no ask/premium to price order")
            order = ws.buy_option(account_id, opt["id"], qty, float(limit))
            store.apply_position(self.mode, alert, qty)
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
