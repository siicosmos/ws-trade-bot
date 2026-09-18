from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionResult:
    ok: bool
    detail: str
    qty: int = 0
    price: Optional[float] = None
    order_id: Optional[str] = None


class PaperExecutor:
    mode = "paper"

    def execute(self, alert, cfg) -> ExecutionResult:
        price = alert.entry
        qty = 0
        if alert.action == "BUY" and price:
            qty = max(0, int(cfg.trading.position_size_cad / price))
        return ExecutionResult(
            ok=True,
            detail=f"[PAPER] {alert.action} {qty} {alert.ticker}"
            + (f" @ {price}" if price else " (no entry price in alert)"),
            qty=qty,
            price=price,
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

    def execute(self, alert, cfg) -> ExecutionResult:
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
                return ExecutionResult(
                    False, "computed sell quantity below 1"
                )

        if cfg.trading.order_type == "limit" and price:
            order = ws.limit_sell(
                account_id, sec_id, held, self._limit_price(price, "SELL")
            )
        else:
            order = ws.market_sell(account_id, sec_id, held)
        return ExecutionResult(
            True,
            f"SELL {held} {alert.ticker} @ ~{price}" if price else f"SELL {held} {alert.ticker}",
            qty=held,
            price=price,
            order_id=str(order.get("orderId") or ""),
        )
