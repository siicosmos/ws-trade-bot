import time
from datetime import datetime, timezone
from typing import List

from .config import WSAccountConfig
from .ws_tokens import persist_env_tokens


def _amount(node) -> float:
    """Amount of a {amount, currency} GraphQL field, 0 when absent."""
    if not node:
        return 0.0
    try:
        return float(node.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


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
        self._pos_cache = None
        self._pos_cache_ts = 0.0
        self._raw_cache = None
        self._raw_ts = 0.0
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
        raw = self._positions_raw(max_age_seconds)
        if raw is None:
            return None
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
                    (sec.get("quoteV2") or {}).get("currency") or ""
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
                market_value = qty * quote if quote else None
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
                        "margin_req_amount": _amount(
                            p.get("marginRequirement")
                        ),
                        "margin_req_currency": (
                            p.get("marginRequirement") or {}
                        ).get("currency"),
                    }
                )
            out[label] = rows
        return out

    def open_option_positions(self, max_age_seconds=None):
        """Real open option positions per account label.

        Returns {label: {"positions": [...], "fx": float|None,
        "usd_cash": float|None}} with amounts split by currency
        (US options quote in USD, the account books in CAD), or
        {label: None} for accounts that could not be fetched so
        callers can fall back to tracked positions. Cached for a
        few minutes.
        """
        raw = self._positions_raw(max_age_seconds)
        if raw is None:
            return None
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
                margin_amount = _amount(margin)
                margin_currency = margin.get("currency")

                legs_data = p.get("legs") or []
                strategy_type = str(
                    p.get("strategyType") or ""
                ).strip()
                if legs_data and strategy_type:
                    # WS already combined a multi-leg order into one
                    # node - build the spread row directly from the
                    # node-level net values instead of re-grouping
                    try:
                        qty = abs(float(p.get("quantity") or 0))
                    except (TypeError, ValueError):
                        qty = 0
                    # signed: credit spreads carry negative net books
                    cost_cad = book
                    cost_usd = market_book
                    market_value = _amount(p.get("totalValue"))
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
                            "short": False,
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
                                if market_value else None
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
                market_value = qty * quote * multiplier if quote else None
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
            # multi-leg positions (spreads) collapse into one row:
            # net cost, combined market value, and risk that reflects
            # the defined-loss structure instead of raw leg sums
            groups = {}
            for r in rows:
                if r.get("strategy_type"):
                    # already combined by WS - keep node-level values
                    continue
                key = (r["underlying"], r["expiry"], r["right"])
                groups.setdefault(key, []).append(r)
            combined = [
                r for r in rows if r.get("strategy_type")
            ]
            for key, legs in groups.items():
                if len(legs) == 1:
                    leg = legs[0]
                    leg["risk_cad"] = (
                        None if leg["short"] else leg["cost_cad"]
                    )
                    combined.append(leg)
                    continue
                legs.sort(key=lambda r: float(r["strike"]))

                def signed(leg, field):
                    value = leg.get(field) or 0
                    return -value if leg["short"] else value

                net_cost_usd = sum(signed(l, "cost_usd") for l in legs)
                net_cost_cad = sum(signed(l, "cost_cad") for l in legs)
                net_mv_usd = sum(signed(l, "market_value") for l in legs)
                qty_set = {abs(l["qty"]) for l in legs}
                qty = qty_set.pop() if len(qty_set) == 1 else max(qty_set)
                strikes = sorted(float(l["strike"]) for l in legs)
                underlying, expiry, right = key
                width = (strikes[-1] - strikes[0]) * 100 * qty
                leg_fx = None
                for l in legs:
                    if l["cost_usd"] and l["cost_cad"]:
                        leg_fx = abs(l["cost_cad"]) / abs(l["cost_usd"])
                        break
                if leg_fx is None:
                    leg_fx = self._usd_cad_quote()
                if net_cost_usd >= 0:
                    risk_usd = net_cost_usd             # debit spread
                else:
                    risk_usd = max(0.0, width - abs(net_cost_usd))
                profit = net_mv_usd - net_cost_usd
                per_unit = net_cost_usd / (qty * 100) if qty else None
                cur_unit = net_mv_usd / (qty * 100) if qty else None
                combined.append(
                    {
                        "contract_key": (
                            f"{underlying} {strikes[0]:g}/"
                            f"{strikes[-1]:g}{right}"
                        ),
                        "underlying": underlying,
                        "expiry": expiry,
                        "strike": f"{strikes[0]:g}/{strikes[-1]:g}",
                        "right": right,
                        "qty": qty,
                        "short": False,
                        "spread": True,
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
                        "pct_return": round(profit / risk_usd * 100, 1)
                        if risk_usd else None,
                    }
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
