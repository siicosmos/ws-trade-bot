"""Paper trading: the seeded ledger and per-account seeding.

Split out of trader/account.py (plan #6) - this module owns the
simulated cash + holdings state and the seeding that mirrors
live long positions. Depends only on ws_common helpers and the
store."""

import time

from ..ws.ws_common import (
    _amount_opt,
    _quote_price,
    account_label,
    effective_accounts,
)


class PaperAccount:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store

    def value(self, label: str = "default") -> float:
        eq = self.store.paper_equity(label)
        if eq is None:
            eq = self._start_value(label)
            self.store.set_paper_equity(eq, label)
        return eq

    def values(self) -> dict:
        return {
            account_label(a): self.value(account_label(a))
            for a in effective_accounts(self.cfg)
        }

    def _start_value(self, label: str) -> float:
        for acct in effective_accounts(self.cfg):
            if account_label(acct) == label and acct.paper_value is not None:
                return float(acct.paper_value)
        return float(self.cfg.trading.paper_account_value)


class PaperLedger:
    """Paper accounts seeded from their real counterparts.

    Cash is seeded so the total equals the account's live value;
    positions mirror the live legs and are marked to market from
    live quotes when available (cost basis otherwise).
    """

    def __init__(self, cfg, store, ws_account=None):
        self.cfg = cfg
        self.store = store
        self.ws_account = ws_account
        self._quote_cache = None
        self._quote_ts = 0.0

    def _quotes(self):
        """{contract_key: price} from live positions, cached."""
        now = time.time()
        refresh = getattr(
            getattr(self.cfg, "wealthsimple", None),
            "positions_refresh_seconds", 30,
        )
        if (
            self._quote_cache is not None
            and now - self._quote_ts < refresh
        ):
            return self._quote_cache
        quotes = {}
        if self.ws_account is not None:
            try:
                raw = self.ws_account._positions_raw() or {}
                for nodes in raw.values():
                    for p in nodes or []:
                        sec = p.get("security") or {}
                        od = sec.get("optionDetails")
                        price = _quote_price(sec.get("quoteV2"))
                        if od:
                            underlying = (
                                ((od.get("underlyingSecurity") or {})
                                 .get("stock") or {}).get("symbol")
                                or (sec.get("stock") or {}).get("symbol")
                                or ""
                            )
                            right = (
                                "C" if str(
                                    od.get("optionType") or ""
                                ).upper().startswith("CALL") else "P"
                            )
                            try:
                                strike = float(od.get("strikePrice"))
                            except (TypeError, ValueError):
                                continue
                            # alerts key expiries as plain dates
                            # (2026-09-24) - the raw graphql value
                            # is a full timestamp, which never
                            # matched and froze paper pricing at
                            # cost basis (return stuck at 0%)
                            expiry = str(
                                od.get("expiryDate") or ""
                            )[:10]
                            key = (
                                f"{underlying}-{expiry}"
                                f"-{strike:g}-{right}"
                            )
                        else:
                            symbol = (
                                (sec.get("stock") or {}).get("symbol")
                                or ""
                            )
                            if not symbol:
                                continue
                            key = symbol
                        if price:
                            if od:
                                usd = True   # option alerts quote USD
                            else:
                                usd = str(
                                    (sec.get("quoteV2") or {})
                                    .get("currency")
                                    or sec.get("currency") or ""
                                ).upper() == "USD"
                            quotes[key] = {"price": price, "usd": usd}
            except Exception:
                pass
        self._quote_cache = quotes
        self._quote_ts = now
        return quotes

    def fx(self) -> float:
        """USD->CAD rate for booking USD option premiums."""
        if self.ws_account is not None:
            try:
                live = self.ws_account.open_option_positions() or {}
                for data in live.values():
                    if data and data.get("fx"):
                        return data["fx"]
            except Exception:
                pass
            hint = getattr(self.ws_account, "_fx_hint", None)
            if hint:
                return hint
        return 1.0

    def _start_value(self, label):
        for acct in effective_accounts(self.cfg):
            if (
                account_label(acct) == label
                and acct.paper_value is not None
            ):
                return float(acct.paper_value)
        return float(
            getattr(
                self.cfg.trading, "paper_account_value", 10000
            )
        )

    def value(self, label: str = "default") -> float:
        cash = self.store.paper_equity(label)
        if cash is None:
            # never seeded (no live data) - fall back to the
            # configured paper start value like the classic ledger
            cash = self._start_value(label)
            self.store.set_paper_equity(cash, label)
        quotes = self._quotes()
        fx = self.fx()
        total = cash
        for pos in self.store.list_positions("paper", label):
            quote = quotes.get(pos["contract_key"])
            if quote is None:
                quote = {
                    "price": pos.get("avg_premium") or 0.0,
                    "usd": pos.get("right") not in (None, "", "?"),
                }
            mult = (
                100 if pos.get("right") not in (None, "", "?") else 1
            )
            total += (
                (pos["qty"] or 0) * quote["price"] * mult
                * (fx if quote.get("usd") else 1.0)
            )
        return round(total, 2)

    def positions(self, label):
        """Detailed paper holdings: live-priced rows with P&L
        against the (fx-converted) cost basis."""
        quotes = self._quotes()
        fx = self.fx()
        rows = []
        for pos in self.store.list_positions("paper", label):
            quote = quotes.get(pos["contract_key"])
            is_option = pos.get("right") not in (None, "", "?")
            if quote is None:
                price = pos.get("avg_premium") or 0.0
                usd = is_option
            else:
                price = quote["price"]
                usd = quote.get("usd", is_option)
            mult = 100 if is_option else 1
            value = (pos["qty"] or 0) * price * mult * (
                fx if usd else 1.0
            )
            cost = (
                (pos["qty"] or 0) * (pos.get("avg_premium") or 0.0)
                * mult * (fx if usd else 1.0)
            )
            pnl = (
                round((value / cost - 1) * 100, 1)
                if cost else None
            )
            rows.append(
                {
                    "contract_key": pos["contract_key"],
                    "underlying": pos.get("underlying"),
                    "kind": "option" if is_option else "stock",
                    "usd": bool(usd),
                    "qty": pos["qty"],
                    "avg": round(pos.get("avg_premium") or 0.0, 4),
                    "value": round(value, 2),
                    "pnl": pnl,
                }
            )
        return rows

    def values(self) -> dict:
        return {
            account_label(a): self.value(account_label(a))
            for a in effective_accounts(self.cfg)
        }


def seed_paper_accounts(cfg, store, ws_account):
    """Seed each paper account from its live counterpart, once.

    Cash is set so cash + seeded positions equals the live value;
    short legs are not tracked (their drift stays on the real
    account) but their value is backed out of the seed.
    """
    seeded = []
    # holdings are only mirrored into paper when real-fill
    # mirroring is on; otherwise the ledger is cash-only at the
    # live account value
    mirror_on = bool(
        getattr(
            getattr(cfg, "paper", None), "mirror", True
        )
    )
    try:
        values = ws_account.values() or {}
        raw = (
            ws_account._positions_raw() or {} if mirror_on else {}
        )
    except Exception:
        return seeded
    for label, account_id in ws_account._resolve():
        if not account_id:
            continue
        if store.meta_get(f"paper_seed:{label}"):
            continue
        value = values.get(label)
        if value is None:
            continue
        nodes = raw.get(label) or []
        positions_value = 0.0
        longs = []
        for p in nodes:
            sec = p.get("security") or {}
            od = sec.get("optionDetails")
            try:
                qty = float(p.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            direction = str(
                p.get("positionDirection") or ""
            ).upper()
            short = direction == "SHORT" or qty < 0
            qty = abs(qty)
            mv = _amount_opt(p.get("totalValue")) or 0.0
            book = _amount_opt(p.get("bookValue"))
            market_book = _amount_opt(p.get("marketBookValue"))
            leg_fx = (
                abs(book) / abs(market_book)
                if book and market_book else None
            )
            if od:
                underlying = (
                    ((od.get("underlyingSecurity") or {})
                     .get("stock") or {}).get("symbol")
                    or (sec.get("stock") or {}).get("symbol")
                    or ""
                )
                right = (
                    "C" if str(od.get("optionType") or "")
                    .upper().startswith("CALL") else "P"
                )
                try:
                    strike = float(od.get("strikePrice"))
                except (TypeError, ValueError):
                    continue
                avg = _amount_opt(p.get("averagePrice"))
                if avg is None and market_book:
                    avg = abs(market_book) / (qty * 100) if qty else None
                # value in CAD for the seed cash
                conv = leg_fx or 1.0
                if not short and qty > 0:
                    positions_value += abs(mv) * conv
                    longs.append(
                        (
                            f"{underlying}-{od.get('expiryDate', '')}"
                            f"-{strike:g}-{right}",
                            underlying, od.get("expiryDate", ""),
                            strike, right, int(qty), avg,
                        )
                    )
            else:
                symbol = (
                    (sec.get("stock") or {}).get("symbol") or ""
                ).strip()
                if not symbol or qty <= 0:
                    continue
                currency = str(
                    (sec.get("quoteV2") or {}).get("currency")
                    or sec.get("currency") or ""
                ).upper()
                conv = leg_fx or 1.0
                positions_value += abs(mv) * (
                    conv if currency == "USD" else 1.0
                )
                avg = _amount_opt(p.get("averagePrice"))
                longs.append(
                    (symbol, symbol, None, None, None,
                     int(qty), avg)
                )
        cash = round(value - positions_value, 2)
        store.set_paper_equity(cash, label)
        for row in longs:
            store.seed_position("paper", label, *row)
        store.meta_set(f"paper_seed:{label}", time.time())
        store.meta_set(f"paper_initial:{label}", value)
        seeded.append(label)
    return seeded
