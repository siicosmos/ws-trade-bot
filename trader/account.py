import time
from datetime import datetime, timezone
from typing import List

from .config import WSAccountConfig
from .strategies import classify_legs
from .ws_tokens import persist_env_tokens


def _amount(node) -> float:
    """Amount of a {amount, currency} GraphQL field, 0 when absent."""
    if not node:
        return 0.0
    try:
        return float(node.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _amount_opt(node):
    """Amount of a {amount, currency} field, None when absent -
    distinguishes a missing value from a real zero."""
    if not node:
        return None
    try:
        return float(node.get("amount"))
    except (TypeError, ValueError):
        return None


def _quote_price(node) -> float:
    """Price of a quoteV2 field ({price, ...}), 0 when absent."""
    if not node:
        return 0.0
    try:
        return float(node.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def effective_accounts(cfg) -> List[WSAccountConfig]:
    accounts = [a for a in cfg.wealthsimple.accounts if a.enabled]
    if accounts:
        return accounts
    return [WSAccountConfig(account_id="", label="default")]


def account_label(acct: WSAccountConfig) -> str:
    return acct.label or acct.account_id or "default"


def resolve_account_id(ws, cfg):
    if cfg.wealthsimple.accounts:
        for acct in cfg.wealthsimple.accounts:
            if acct.account_id and acct.enabled:
                return acct.account_id
    accounts = ws.get_accounts()
    active = [a for a in accounts if a.get("status") == "ACTIVE"]
    pool = active or accounts
    return pool[0]["id"]


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
                            key = (
                                f"{underlying}-{od.get('expiryDate', '')}"
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


class WealthsimpleAccount:
    def __init__(self, cfg, store=None, cache_seconds=None):
        self.cfg = cfg
        self.store = store
        self.cache_seconds = cache_seconds or getattr(
            cfg.wealthsimple, "values_refresh_seconds", 60
        )
        self._cache = None
        self._cache_ts = 0.0
        self._ws = None
        self._resolved = None
        self._stale = {}
        self._raw_cache = None
        self._raw_ts = 0.0
        self._map_cache = None
        self._fx_quote = None
        self._fx_quote_ts = 0.0
        self._usd_cache = None
        self._usd_cache_ts = 0.0
        self._funding_cache = None
        self._funding_ts = 0.0
        self._type_map = None
        self._margin_rates = {}

    def _client(self):
        if self._ws is None:
            from wealthsimple_python import WealthsimpleV2

            self._ws = WealthsimpleV2()
        return self._ws

    def security_margin_rate(self, security_id):
        """Per-security margin rate via the app's FetchSecurity
        document, cached for 12h. Returns None when unavailable -
        callers fall back to their configured default.
        """
        if not security_id:
            return None
        rates = getattr(self, "_margin_rates", None)
        if rates is None:
            rates = self._margin_rates = {}
        cached = rates.get(security_id)
        now = time.time()
        if cached and now - cached[1] < 12 * 3600:
            return cached[0]
        rate = None
        try:
            ws = self._client()
            from trader.ws_security_query import (
                FETCH_SECURITY, security_variables,
            )

            result = ws.graphql_query(
                "FetchSecurity",
                FETCH_SECURITY,
                security_variables(security_id),
            )
            raw = (
                ((result.get("data") or {}).get("security") or {})
                .get("marginRates") or {}
            ).get("clientMarginRate")
            if raw is not None:
                rate = float(raw)
                if rate > 1:      # tolerate percent-style values
                    rate = rate / 100.0
        except Exception:
            rate = None
        rates[security_id] = (rate, now)
        return rate

    def account_type_map(self):
        """{account_id: unifiedAccountType}, cached (types are static).

        Registered plans (RRSP/TFSA/...) report a "margin
        requirement" that is really cash collateral - callers use
        this to suppress it there.
        """
        if self._type_map is not None:
            return self._type_map
        out = {}
        try:
            ws = self._client()
            for a in ws.get_accounts() or []:
                if a.get("id"):
                    out[a["id"]] = str(
                        a.get("unifiedAccountType") or ""
                    ).upper()
        except Exception:
            pass
        self._type_map = out
        return out

    def _resolve(self):
        if self._resolved is None:
            out = []
            for acct in effective_accounts(self.cfg):
                out.append((account_label(acct), acct.account_id or None))
            if not out or all(not account_id for _, account_id in out):
                try:
                    ws = self._client()
                    first = resolve_account_id(ws, self.cfg)
                    out = [("default", first)]
                except Exception:
                    out = [("default", None)]
            self._resolved = out
        return self._resolved

    def funding_balances(self):
        """Available trading cash per currency, per account label.

        Returns {label: [{"currency": "CAD", "amount": x}, ...]} or
        {label: None} for accounts that could not be fetched. For a
        margin account WS reports its available trading funds here,
        which is effectively its buying power. Cached like values.
        """
        now = time.time()
        if (
            self._funding_cache is not None
            and now - self._funding_ts < self.cache_seconds
        ):
            return self._funding_cache
        try:
            ws = self._client()
        except Exception:
            return None
        out = {}
        any_ok = False
        for label, account_id in self._resolve():
            if not account_id:
                out[label] = None
                continue
            try:
                balances = ws.get_account_funding_balances(
                    [account_id]
                ) or []
                entry = balances[0] if balances else {}
                out[label] = [
                    {
                        "currency": str(b.get("currency") or "").upper(),
                        "amount": float(b.get("amount") or 0),
                    }
                    for b in (entry.get("trading_balances") or [])
                ]
                any_ok = True
            except Exception:
                out[label] = None
        result = out if any_ok else None
        self._funding_cache = result
        self._funding_ts = now
        return result

    def usd_values(self):
        """Net liquidation converted to USD by Wealthsimple itself.

        Returns {label: usd_amount} or None when unavailable; cached
        on the same cadence as CAD values.
        """
        now = time.time()
        if (
            self._usd_cache is not None
            and now - self._usd_cache_ts < self.cache_seconds
        ):
            return self._usd_cache
        try:
            ws = self._client()
        except Exception:
            return None
        out = {}
        any_ok = False
        for label, account_id in self._resolve():
            if not account_id:
                out[label] = None
                continue
            try:
                fin = ws.get_account_current_financials(
                    account_id, currency="USD"
                )
                nlv = fin.get("netLiquidationValueV2") or {}
                amount = float(nlv.get("amount") or 0)
                if nlv.get("currency") == "USD" and amount > 0:
                    out[label] = amount
                    any_ok = True
                else:
                    out[label] = None
            except Exception:
                out[label] = None
        result = out if any_ok else None
        self._usd_cache = result
        self._usd_cache_ts = now
        return result

    def _usd_cad_quote(self):
        """USD:CAD rate from the WS quote API, cached for an hour."""
        now = time.time()
        if (
            self._fx_quote
            and now - self._fx_quote_ts < 3600
        ):
            return self._fx_quote
        try:
            ws = self._client()
            rate = None
            for sec in ws.search_securities("USD:CAD") or []:
                sid = sec.get("id") or (
                    (sec.get("security") or {}).get("id")
                )
                if not sid:
                    continue
                quote = ws.get_security_quote(sid) or {}
                price = (
                    quote.get("price")
                    or (quote.get("amount") or {}).get("amount")
                    if isinstance(quote.get("amount"), dict)
                    else quote.get("amount")
                    or quote.get("lastPrice")
                )
                if price:
                    rate = float(price)
                    break
            if rate and 0.5 < rate < 2.5:
                self._fx_quote = rate
                self._fx_quote_ts = now
                return rate
        except Exception:
            pass
        return None

    def _positions_raw(self, max_age_seconds=None):
        """Raw get_positions per account label, cached briefly."""
        if max_age_seconds is None:
            max_age_seconds = getattr(
                self.cfg.wealthsimple, "positions_refresh_seconds", 30
            )
        now = time.time()
        raw_cache = getattr(self, "_raw_cache", None)
        raw_ts = getattr(self, "_raw_ts", 0.0)
        if raw_cache is not None and now - raw_ts < max_age_seconds:
            return raw_cache
        try:
            ws = self._client()
        except Exception:
            return None
        out = {}
        for label, account_id in self._resolve():
            if not account_id:
                out[label] = None
                continue
            nodes = None
            # the app's own document first: it exposes
            # marginRequirement / strategyType / legs that the
            # library's minimal variant does not
            try:
                from trader.ws_positions_query import (
                    FETCH_IDENTITY_POSITIONS, app_positions_variables,
                )

                variables = app_positions_variables(ws, [account_id])
                if variables.get("identityId"):
                    result = ws.graphql_query(
                        "FetchIdentityPositions",
                        FETCH_IDENTITY_POSITIONS,
                        variables,
                    )
                    edges = (
                        ((result.get("data") or {})
                         .get("identity") or {})
                        .get("financials") or {}
                    ).get("current", {}).get(
                        "positions", {}
                    ).get("edges", [])
                    nodes = [e.get("node") for e in edges]
            except Exception:
                nodes = None
            if nodes is None:
                try:
                    nodes = ws.get_positions(
                        account_ids=[account_id]
                    ) or []
                except Exception:
                    out[label] = None
                    continue
            out[label] = nodes
        self._raw_cache = out
        self._raw_ts = now
        return out

    def stock_holdings(self, max_age_seconds=None):
        """Non-option holdings (stocks/ETFs) per account label.

        Same shape and currency handling as option positions: the
        market-currency amounts in cost_usd/current_price, the CAD
        book value in cost_cad. Currency positions (cash) are
        excluded - they surface in the funding balances.
        """
        mapped = self._mapped_positions(max_age_seconds)
        if mapped is None:
            return None
        return mapped[1]

    def _map_stocks(self, raw):
        out = {}
        for label, positions in raw.items():
            if positions is None:
                out[label] = None
                continue
            rows = []
            for p in positions:
                sec = p.get("security") or {}
                if sec.get("optionDetails"):
                    continue
                if (sec.get("securityType") or "").upper() == "CURRENCY":
                    continue
                stock = sec.get("stock") or {}
                symbol = str(stock.get("symbol") or "").strip()
                if not symbol:
                    continue
                try:
                    qty = float(p.get("quantity") or 0)
                except (TypeError, ValueError):
                    qty = 0
                direction = str(
                    p.get("positionDirection") or ""
                ).upper()
                is_short = direction == "SHORT" or qty < 0
                qty = abs(qty)
                if qty <= 0:
                    continue
                book = _amount(p.get("bookValue"))
                market_book = _amount(p.get("marketBookValue"))
                cost_cad = abs(book)
                quote = _quote_price(sec.get("quoteV2"))
                currency = str(
                    (sec.get("quoteV2") or {}).get("currency")
                    or sec.get("currency") or ""
                ).upper() or None
                # raw averagePrice is the native per-unit cost; the
                # currency-override variants (marketAveragePrice,
                # marketBookValue) convert at today's fx and skew the
                # average. Ignore a cross-currency average (would need
                # purchase-date fx).
                avg_obj = p.get("averagePrice") or {}
                per_unit = abs(_amount(avg_obj))
                avg_cur = str(
                    avg_obj.get("currency") or ""
                ).upper() or None
                if (
                    per_unit
                    and avg_cur
                    and currency
                    and avg_cur != currency
                ):
                    per_unit = None
                if not per_unit and market_book:
                    per_unit = abs(market_book) / qty
                # native total cost from the true average
                cost_usd = qty * per_unit if per_unit else None
                # the node's own totalValue beats qty x quote - a
                # missing quote must not zero the holding out
                total = _amount_opt(p.get("totalValue"))
                market_value = (
                    abs(total) if total is not None
                    else (qty * quote if quote else None)
                )
                pct_return = None
                if market_value and cost_usd:
                    if is_short:
                        pct_return = round(
                            (cost_usd - market_value) / cost_usd * 100, 1
                        )
                    else:
                        pct_return = round(
                            (market_value / cost_usd - 1) * 100, 1
                        )
                rows.append(
                    {
                        "contract_key": symbol,
                        "underlying": symbol,
                        "security_id": sec.get("id"),
                        "expiry": None,
                        "strike": None,
                        "right": None,
                        "qty": qty,
                        "short": is_short,
                        "kind": "stock",
                        "currency": currency,
                        "avg_premium": round(per_unit, 4)
                        if per_unit is not None else None,
                        "cost": cost_usd if cost_usd else cost_cad,
                        "cost_usd": cost_usd,
                        "cost_cad": cost_cad,
                        "current_price": quote or None,
                        "market_value": (
                            round(market_value, 2)
                            if market_value else None
                        ),
                        "pct_return": pct_return,
                        "margin_req_amount": _amount_opt(
                            p.get("marginRequirement")
                        ),
                        "margin_req_currency": (
                            p.get("marginRequirement") or {}
                        ).get("currency"),
                    }
                )
            out[label] = rows
        return out

    def _mapped_positions(self, max_age_seconds=None):
        """(options, stocks) for all labels, mapped once per raw
        fetch generation - the dashboard polls faster than the
        positions refresh, so the mapping is reused.
        """
        raw = self._positions_raw(max_age_seconds)
        if raw is None:
            return None
        cache = getattr(self, "_map_cache", None)
        raw_ts = getattr(self, "_raw_ts", None)
        if cache is not None and cache[0] == raw_ts:
            return cache[1], cache[2]
        options = self._map_options(raw)
        stocks = self._map_stocks(raw)
        self._map_cache = (raw_ts, options, stocks)
        return options, stocks

    def open_option_positions(self, max_age_seconds=None):
        """Real open option positions per account label.

        Returns {label: {"positions": [...], "fx": float|None,
        "usd_cash": float|None}} with amounts split by currency
        (US options quote in USD, the account books in CAD), or
        {label: None} for accounts that could not be fetched so
        callers can fall back to tracked positions.
        """
        mapped = self._mapped_positions(max_age_seconds)
        if mapped is None:
            return None
        return mapped[0]

    def _map_options(self, raw):
        out = {}
        for label, positions in raw.items():
            if positions is None:
                out[label] = None
                continue
            rows = []
            fx = None
            usd_cash = None
            for p in positions or []:
                sec = p.get("security") or {}
                book = _amount(p.get("bookValue"))
                market_book = _amount(p.get("marketBookValue"))

                if (sec.get("securityType") or "").upper() == "CURRENCY":
                    symbol = str(
                        (sec.get("stock") or {}).get("symbol") or ""
                    ).upper()
                    if "USD" in symbol:
                        try:
                            usd_cash = float(p.get("quantity") or 0)
                        except (TypeError, ValueError):
                            usd_cash = None
                        quote = _quote_price(sec.get("quoteV2"))
                        if quote:
                            fx = quote
                    continue

                margin = p.get("marginRequirement") or {}
                margin_amount = _amount_opt(margin)
                margin_currency = margin.get("currency")

                direction = str(
                    p.get("positionDirection") or ""
                ).upper()
                legs_data = p.get("legs") or []
                strategy_type = str(
                    p.get("strategyType") or ""
                ).strip()
                if legs_data and strategy_type:
                    # WS already combined a multi-leg order into one
                    # node - build the spread row directly from the
                    # node-level net values instead of re-grouping
                    try:
                        raw_qty = float(p.get("quantity") or 0)
                    except (TypeError, ValueError):
                        raw_qty = 0
                    qty = abs(raw_qty)
                    net_short = (
                        direction == "SHORT" or raw_qty < 0
                    )
                    # signed: credit spreads carry negative net books
                    cost_cad = book
                    cost_usd = market_book
                    market_value = _amount_opt(p.get("totalValue"))
                    if market_value is None:
                        # expired positions can drop the node value -
                        # fall back to the legs' own totals
                        legs_total = None
                        for leg in legs_data:
                            lt = _amount_opt(leg.get("totalValue"))
                            if lt is not None:
                                legs_total = (
                                    lt if legs_total is None
                                    else legs_total + lt
                                )
                        market_value = legs_total
                    strikes = []
                    expiry = None
                    right = ""
                    underlying = ""
                    for leg in legs_data:
                        lsec = leg.get("security") or {}
                        lod = lsec.get("optionDetails") or {}
                        if not lod:
                            continue
                        expiry = expiry or lod.get("expiryDate")
                        if lod.get("strikePrice") is not None:
                            try:
                                strikes.append(
                                    float(lod.get("strikePrice"))
                                )
                            except (TypeError, ValueError):
                                pass
                        if not right:
                            rt = str(
                                lod.get("optionType") or ""
                            ).upper()
                            if rt:
                                right = (
                                    "C" if rt.startswith("CALL")
                                    else "P"
                                )
                        underlying = underlying or (
                            ((lod.get("underlyingSecurity") or {})
                             .get("stock") or {}).get("symbol")
                        )
                    strikes.sort()
                    # name the structure from its legs when we can
                    # (richer than WS's raw VERTICAL_SPREAD tag)
                    leg_infos = []
                    for leg in legs_data:
                        lod = (leg.get("security") or {}).get(
                            "optionDetails"
                        ) or {}
                        if not lod:
                            continue
                        try:
                            lqty = abs(float(leg.get("quantity") or qty))
                        except (TypeError, ValueError):
                            lqty = qty
                        lshort = str(
                            leg.get("positionDirection") or ""
                        ).upper() == "SHORT"
                        leg_infos.append(
                            {
                                "strike": lod.get("strikePrice"),
                                "right": (
                                    "C" if str(
                                        lod.get("optionType") or ""
                                    ).upper().startswith("CALL")
                                    else "P"
                                ),
                                "short": lshort,
                                "qty": lqty,
                            }
                        )
                    named = classify_legs(leg_infos) or {}
                    if named.get("name"):
                        strategy_type = named["name"]
                    leg_fx = (
                        cost_cad / cost_usd
                        if cost_usd and cost_cad else None
                    )
                    risk_cad = None
                    risk_m = margin_amount
                    if risk_m is None and cost_usd is not None:
                        # debit spread: the debit is the max loss
                        if not market_value or market_value <= cost_usd:
                            risk_m = abs(cost_usd)
                    if risk_m is not None:
                        risk_cad = round(
                            risk_m * (
                                leg_fx if (
                                    margin_currency == "USD"
                                    and leg_fx
                                ) else 1
                            ), 2,
                        )
                    market_pct = None
                    if market_value is not None and cost_usd:
                        profit = market_value - cost_usd
                        if risk_m:
                            market_pct = round(
                                profit / risk_m * 100, 1
                            )
                        elif cost_usd:
                            market_pct = round(
                                profit / abs(cost_usd) * 100, 1
                            )
                    per_unit = (
                        cost_usd / (qty * 100)
                        if cost_usd and qty else None
                    )
                    rows.append(
                        {
                            "contract_key": (
                                f"{underlying} {expiry or ''} "
                                f"{strikes[0]:g}/{strikes[-1]:g}"
                                f"{right}" if strikes else
                                f"{underlying} {expiry or ''}{right}"
                            ),
                            "underlying": underlying,
                            "expiry": expiry,
                            "strike": (
                                f"{strikes[0]:g}/{strikes[-1]:g}"
                                if strikes else None
                            ),
                            "right": right or None,
                            "qty": qty,
                            "short": net_short,
                            "spread": True,
                            "strategy_type": strategy_type,
                            "avg_premium": round(per_unit, 4)
                            if per_unit is not None else None,
                            "cost": cost_usd if cost_usd is not None
                            else book,
                            "cost_usd": cost_usd,
                            "cost_cad": cost_cad,
                            "risk_cad": risk_cad,
                            "current_price": None,
                            "market_value": (
                                round(market_value, 2)
                                if market_value is not None else None
                            ),
                            "pct_return": market_pct,
                            "margin_req_amount": margin_amount,
                            "margin_req_currency": margin_currency,
                        }
                    )
                    continue

                od = sec.get("optionDetails")
                if not od:
                    continue
                try:
                    qty = float(p.get("quantity") or 0)
                except (TypeError, ValueError):
                    qty = 0
                direction = str(p.get("positionDirection") or "").upper()
                is_short = direction == "SHORT" or qty < 0
                qty = abs(qty)
                if qty <= 0:
                    continue
                underlying = (
                    ((od.get("underlyingSecurity") or {})
                     .get("stock") or {}).get("symbol")
                    or (sec.get("stock") or {}).get("symbol")
                    or ""
                )
                multiplier = float(od.get("multiplier") or 100) or 100
                # shorts carry negative/credit book values; use magnitudes
                cost_cad = abs(book)
                cost_usd = abs(market_book)
                if cost_usd and cost_cad:
                    fx = cost_cad / cost_usd
                quote = _quote_price(sec.get("quoteV2"))  # USD for US options
                if not cost_usd and book and fx:
                    cost_usd = book / fx
                per_unit_usd = (
                    cost_usd / (qty * multiplier)
                    if cost_usd else None
                )
                right = (
                    "C"
                    if str(od.get("optionType") or "").upper().startswith(
                        "CALL"
                    )
                    else "P"
                )
                total = _amount_opt(p.get("totalValue"))
                market_value = (
                    abs(total) if total is not None
                    else (qty * quote * multiplier if quote else None)
                )
                pct_return = None
                if market_value and cost_usd:
                    if is_short:
                        # short: credit received minus buyback cost,
                        # relative to the credit
                        pct_return = round(
                            (cost_usd - market_value)
                            / cost_usd * 100,
                            1,
                        )
                    else:
                        pct_return = round(
                            (market_value / cost_usd - 1) * 100, 1
                        )
                rows.append(
                    {
                        "contract_key": (
                            f"{underlying} {od.get('expiryDate', '')} "
                            f"{od.get('strikePrice')}{right}"
                        ),
                        "underlying": underlying,
                        "expiry": od.get("expiryDate"),
                        "strike": od.get("strikePrice"),
                        "right": right,
                        "qty": qty,
                        "short": is_short,
                        "avg_premium": round(per_unit_usd, 4)
                        if per_unit_usd is not None else None,
                        "cost": cost_usd if cost_usd is not None else book,
                        "cost_usd": cost_usd,
                        "cost_cad": cost_cad,
                        "current_price": quote or None,
                        "market_value": (
                            round(market_value, 2)
                            if market_value else None
                        ),
                        "pct_return": pct_return,
                        "margin_req_amount": margin_amount,
                        "margin_req_currency": margin_currency,
                    }
                )
            if fx:
                self._fx_hint = fx
            # multi-leg positions collapse into one row per strategy:
            # verticals, butterflies, condors and iron butterflies
            # are named from their legs and risked by structure
            def _strategy_row(underlying, expiry, legs):
                def signed(leg, field):
                    value = leg.get(field) or 0
                    return -value if leg["short"] else value

                net_cost_usd = sum(
                    signed(l, "cost_usd") for l in legs
                )
                net_cost_cad = sum(
                    signed(l, "cost_cad") for l in legs
                )
                net_mv_usd = sum(
                    signed(l, "market_value") for l in legs
                )
                qty_set = {abs(l["qty"]) for l in legs}
                qty = (
                    qty_set.pop() if len(qty_set) == 1
                    else max(qty_set)
                )
                leg_fx = None
                for l in legs:
                    if l["cost_usd"] and l["cost_cad"]:
                        leg_fx = (
                            abs(l["cost_cad"])
                            / abs(l["cost_usd"])
                        )
                        break
                if leg_fx is None:
                    leg_fx = self._usd_cad_quote()

                info = classify_legs(
                    [
                        {
                            "strike": l["strike"],
                            "right": l["right"],
                            "short": l["short"],
                            "qty": l["qty"],
                        }
                        for l in legs
                    ]
                )
                strikes = sorted(
                    float(l["strike"]) for l in legs
                )
                if info is None:
                    info = {
                        "name": None, "kind": "vertical",
                        "qty": qty,
                        "strikes": strikes,
                        "width": strikes[-1] - strikes[0],
                        "debit": None,
                    }
                qty = info.get("qty") or qty
                width = (info.get("width") or 0) * 100 * qty
                if info.get("kind") in (
                    "condor", "iron_fly", "butterfly"
                ):
                    # defined-risk multi-wing structures: debit is
                    # the max loss when bought, otherwise the
                    # widest wing minus the credit
                    if net_cost_usd >= 0:
                        risk_usd = net_cost_usd
                    else:
                        risk_usd = max(
                            0.0, width - abs(net_cost_usd)
                        )
                elif net_cost_usd >= 0:
                    risk_usd = net_cost_usd     # debit vertical
                else:
                    risk_usd = max(
                        0.0, width - abs(net_cost_usd)
                    )
                profit = net_mv_usd - net_cost_usd
                per_unit = (
                    net_cost_usd / (qty * 100) if qty else None
                )
                cur_unit = (
                    net_mv_usd / (qty * 100) if qty else None
                )
                rights = {l["right"] for l in legs}
                right = (
                    next(iter(rights)) if len(rights) == 1 else None
                )
                strike_str = "/".join(
                    f"{s:g}" for s in info.get("strikes") or strikes
                )
                return {
                    "contract_key": (
                        f"{underlying} {strike_str}"
                        f"{right or 'C/P'}"
                    ),
                    "underlying": underlying,
                    "expiry": expiry,
                    "strike": strike_str,
                    "right": right,
                    "qty": qty,
                    # net-short (credit) structures are sold - show
                    # the negative quantity and direction
                    "short": net_cost_usd < 0,
                    "spread": True,
                    "strategy_type": info.get("name"),
                    "avg_premium": round(per_unit, 4)
                    if per_unit is not None else None,
                    "cost": net_cost_usd,
                    "cost_usd": net_cost_usd,
                    "cost_cad": net_cost_cad,
                    "risk_cad": round(risk_usd * leg_fx, 2)
                    if (leg_fx and risk_usd) else None,
                    "current_price": round(cur_unit, 4)
                    if cur_unit is not None else None,
                    "market_value": round(net_mv_usd, 2),
                    # return on the premium: an expired credit
                    # structure keeps its full credit (+100%)
                    "pct_return": round(
                        profit / abs(net_cost_usd) * 100, 1
                    )
                    if net_cost_usd else None,
                }

            groups = {}
            for r in rows:
                if r.get("strategy_type"):
                    # already combined by WS - keep node-level values
                    continue
                key = (r["underlying"], r["expiry"])
                groups.setdefault(key, []).append(r)
            combined = [
                r for r in rows if r.get("strategy_type")
            ]
            for (underlying, expiry), legs in groups.items():
                if len(legs) == 1:
                    leg = legs[0]
                    leg["risk_cad"] = (
                        None if leg["short"] else leg["cost_cad"]
                    )
                    combined.append(leg)
                    continue
                if len({l["right"] for l in legs}) > 1:
                    # mixed call/put legs: condor / iron butterfly
                    combined.append(
                        _strategy_row(underlying, expiry, legs)
                    )
                    continue
                legs = sorted(
                    legs, key=lambda r: float(r["strike"])
                )
                combined.append(
                    _strategy_row(underlying, expiry, legs)
                )
            rows = combined

            if fx is None:
                fx = self._usd_cad_quote()
            out[label] = {
                "positions": rows,
                "fx": fx,
                "usd_cash": usd_cash,
            }
        return out

    def _try_fetch(self):
        try:
            ws = self._client()
        except Exception:
            return None
        result = {}
        any_ok = False
        for label, account_id in self._resolve():
            if not account_id:
                result[label] = None
                continue
            try:
                fin = ws.get_account_current_financials(account_id)
                result[label] = float(fin["netLiquidationValueV2"]["amount"])
                any_ok = True
            except Exception:
                result[label] = None
        return result if any_ok else None

    def _load_cached(self):
        if not self.store:
            return None
        out = {}
        for label, _ in self._resolve():
            cached = self.store.get_cached_value(label)
            if cached and cached.get("value") is not None:
                out[label] = cached["value"]
                self._stale[label] = cached.get("ts")
        return out or None

    def stale_age(self, label: str):
        ts = self._stale.get(label)
        if not ts:
            return None
        try:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None
        if delta.total_seconds() < 0:
            return None
        seconds = int(delta.total_seconds())
        if seconds < 90:
            return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 90:
            return f"{minutes}m"
        hours = minutes // 60
        if hours < 36:
            return f"{hours}h"
        return f"{hours // 24}d"

    def values(self) -> dict:
        now = time.time()
        if self._cache is not None and now - self._cache_ts < self.cache_seconds:
            return self._cache

        result = self._try_fetch()
        if result is not None:
            self._cache = result
            self._cache_ts = now
            self._stale = {}
            if self.store is not None:
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for label, v in result.items():
                    if v is not None:
                        self.store.set_cached_value(label, v, ts)
            persist_env_tokens()
            return result

        cached = self._load_cached()
        if cached:
            self._cache = cached
            self._cache_ts = now
            return cached

        raise RuntimeError(
            "could not fetch Wealthsimple values and no cached value available"
        )

    def value(self, label: str = "default"):
        return self.values().get(label)
