import time
from datetime import datetime, timezone
from typing import List

from .config import WSAccountConfig
from .ws_tokens import persist_env_tokens


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

    def _client(self):
        if self._ws is None:
            from wealthsimple_python import WealthsimpleV2

            self._ws = WealthsimpleV2()
        return self._ws

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

    def open_option_positions(self, max_age_seconds=None):
        """Real open option positions per account label.

        Returns {label: [position-dicts]} or {label: None} for accounts
        that could not be fetched, so callers can fall back to tracked
        positions. Cached for a few minutes.
        """
        if max_age_seconds is None:
            max_age_seconds = getattr(
                self.cfg.wealthsimple, "positions_refresh_seconds", 30
            )
        now = time.time()
        if (
            self._pos_cache is not None
            and now - self._pos_cache_ts < max_age_seconds
        ):
            return self._pos_cache
        try:
            ws = self._client()
        except Exception:
            return None
        out = {}
        for label, account_id in self._resolve():
            if not account_id:
                out[label] = None
                continue
            try:
                positions = ws.get_positions(account_ids=[account_id])
            except Exception:
                out[label] = None
                continue
            rows = []
            for p in positions or []:
                sec = p.get("security") or {}
                od = sec.get("optionDetails")
                if not od:
                    continue
                try:
                    qty = float(p.get("quantity") or 0)
                except (TypeError, ValueError):
                    qty = 0
                if qty <= 0:
                    continue
                underlying = (
                    ((od.get("underlyingSecurity") or {})
                     .get("stock") or {}).get("symbol")
                    or (sec.get("stock") or {}).get("symbol")
                    or ""
                )
                book = float(
                    (p.get("bookValue") or {}).get("amount") or 0
                )
                multiplier = float(od.get("multiplier") or 100) or 100
                try:
                    avg = float(
                        (p.get("averagePrice") or {}).get("amount") or 0
                    )
                except (TypeError, ValueError):
                    avg = 0
                per_unit = (
                    book / (qty * multiplier)
                    if book and qty else avg / multiplier
                )
                right = (
                    "C"
                    if str(od.get("optionType") or "").upper().startswith(
                        "CALL"
                    )
                    else "P"
                )
                quote = 0
                try:
                    quote = float(
                        (sec.get("quoteV2") or {}).get("price") or 0
                    )
                except (TypeError, ValueError):
                    quote = 0
                market_value = qty * quote * multiplier if quote else None
                pct_return = None
                if market_value and book:
                    pct_return = round(
                        (market_value / book - 1) * 100, 1
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
                        "avg_premium": round(per_unit, 4),
                        "cost": book,
                        "current_price": quote or None,
                        "market_value": (
                            round(market_value, 2)
                            if market_value else None
                        ),
                        "pct_return": pct_return,
                    }
                )
            out[label] = rows
        self._pos_cache = out
        self._pos_cache_ts = now
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
