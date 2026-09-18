import time
from typing import List

from .config import WSAccountConfig


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
    def __init__(self, cfg, cache_seconds=900):
        self.cfg = cfg
        self.cache_seconds = cache_seconds
        self._cache = None
        self._cache_ts = 0.0
        self._ws = None
        self._resolved = None

    def _client(self):
        if self._ws is None:
            from wealthsimple_python import WealthsimpleV2

            self._ws = WealthsimpleV2()
        return self._ws

    def _resolve(self):
        if self._resolved is None:
            out = []
            for acct in effective_accounts(self.cfg):
                out.append((account_label(acct), acct.account_id))
            if not out or all(not account_id for _, account_id in out):
                ws = self._client()
                out = [("default", resolve_account_id(ws, self.cfg))]
            self._resolved = out
        return self._resolved

    def values(self) -> dict:
        now = time.time()
        if self._cache is not None and now - self._cache_ts < self.cache_seconds:
            return self._cache
        ws = self._client()
        result = {}
        for label, account_id in self._resolve():
            try:
                fin = ws.get_account_current_financials(account_id)
                result[label] = float(fin["netLiquidationValueV2"]["amount"])
            except Exception:
                result[label] = None
        if all(v is None for v in result.values()):
            raise RuntimeError(
                "could not fetch account values from Wealthsimple"
            )
        self._cache = result
        self._cache_ts = now
        return result

    def value(self, label: str = "default"):
        return self.values().get(label)
