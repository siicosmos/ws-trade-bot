import time


def resolve_account_id(ws, cfg):
    if cfg.wealthsimple.account_id:
        return cfg.wealthsimple.account_id
    accounts = ws.get_accounts()
    active = [a for a in accounts if a.get("status") == "ACTIVE"]
    pool = active or accounts
    return pool[0]["id"]


class PaperAccount:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store

    def value(self) -> float:
        eq = self.store.paper_equity()
        if eq is None:
            eq = float(self.cfg.trading.paper_account_value)
            self.store.set_paper_equity(eq)
        return eq


class WealthsimpleAccount:
    def __init__(self, cfg, cache_seconds=900):
        self.cfg = cfg
        self.cache_seconds = cache_seconds
        self._cache = None
        self._cache_ts = 0.0
        self._account_id = None
        self._ws = None

    def _client(self):
        if self._ws is None:
            from wealthsimple_python import WealthsimpleV2

            self._ws = WealthsimpleV2()
        return self._ws

    def value(self) -> float:
        now = time.time()
        if self._cache is not None and now - self._cache_ts < self.cache_seconds:
            return self._cache
        ws = self._client()
        if self._account_id is None:
            self._account_id = resolve_account_id(ws, self.cfg)
        try:
            fin = ws.get_account_current_financials(self._account_id)
            value = float(fin["netLiquidationValueV2"]["amount"])
        except Exception:
            fins = ws.get_account_financials([self._account_id])
            value = float(fins[0]["netWorth"]["amount"])
        self._cache = value
        self._cache_ts = now
        return value
