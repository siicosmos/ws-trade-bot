import time
from datetime import datetime, timezone
from typing import List

from ..config import WSAccountConfig
from ..trading.strategies import classify_legs
from .ws_tokens import persist_env_tokens
from ..trading.paper import (  # noqa: F401 - re-exported
    PaperAccount,
    PaperLedger,
    seed_paper_accounts,
)


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
            from trader.ws.ws_security_query import (
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
                from trader.ws.ws_positions_query import (
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
        from .mapping import map_stocks

        return map_stocks(raw)

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
        from .mapping import map_options

        out = map_options(raw, self._usd_cad_quote)
        for data in out.values():
            if data and data.get("fx"):
                self._fx_hint = data["fx"]
                break
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
