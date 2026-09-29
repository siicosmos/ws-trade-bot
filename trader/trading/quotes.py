import time

from .parser import Alert

# the live quote provider, held for the web layer (the spx
# levels ladder polls the index spot through it)
ACTIVE_QUOTE_PROVIDER = None


def make_quote_provider(cfg, account):
    if not cfg.quotes.enabled:
        print(
            "live option quotes disabled (quotes.enabled=false) - "
            "stop monitor off"
        )
        return None
    provider = (cfg.quotes.provider or "ws").lower()
    if provider == "moomoo":
        global ACTIVE_QUOTE_PROVIDER
        try:
            moomoo = MoomooQuoteProvider(cfg)
            moomoo._context()
            ACTIVE_QUOTE_PROVIDER = moomoo
            print(
                f"quotes: moomoo OpenD at {cfg.quotes.moomoo_host}:"
                f"{cfg.quotes.moomoo_port}"
            )
            return moomoo
        except Exception as e:
            print(
                f"quotes: moomoo unavailable ({e}) - falling back to "
                f"Wealthsimple option chains"
            )
    return make_ws_quote_provider(cfg, account)


def make_ws_quote_provider(cfg, account):
    try:
        from .executor import WealthsimpleExecutor

        resolver = WealthsimpleExecutor(cfg, account)
        ws = resolver._client()
        ws.get_accounts()

        def quote(pos):
            sec_id = resolver._resolve_security(ws, pos["underlying"])
            if not sec_id:
                return None
            alert = Alert(
                action="SELL",
                ticker=pos["underlying"],
                kind="option",
                underlying=pos["underlying"],
                expiry=pos["expiry"],
                strike=pos["strike"],
                right=pos["right"],
            )
            opt, _ = resolver._resolve_option(ws, sec_id, alert)
            if not opt:
                return None
            return (opt.get("quote") or {}).get("bid")

        print("quotes: Wealthsimple option chains")
        return quote
    except Exception as e:
        print(
            f"quotes: Wealthsimple unavailable ({e}) - run "
            f"scripts/ws_login.py to enable auto stop-losses"
        )
        return None


class MoomooQuoteProvider:
    def __init__(self, cfg):
        self.cfg = cfg
        self._ctx = None
        self._index_cache = None
        self._index_ts = 0.0
        self._index_proxy = False
        # the last positive spot: outside trading hours the
        # snapshot returns nothing positive, but the ladder
        # should still show the last close (marked stale)
        self._index_last = None
        self._stock_cache = {}   # symbol -> (ts, price)

    def stock_quote(self, symbol, ttl=5.0):
        """Realtime stock/etf spot from the snapshot, cached
        briefly like the index quote. US stocks keep trading in
        the overnight and post-market sessions, so this stays
        live when the index itself is closed - the spy ladder
        rides it instead of the slow ws quote api."""
        sym = str(symbol or "").upper()
        if not sym:
            return None
        now = time.time()
        hit = self._stock_cache.get(sym)
        if hit and now - hit[0] < ttl:
            return hit[1]
        try:
            ret, data = self._context().get_market_snapshot(
                [f"US.{sym}"]
            )
        except Exception:
            # OpenD can drop the connection (restart, machine
            # sleep) - drop the cached context so the next poll
            # reconnects instead of staying blind
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None
            return None
        if ret != 0 or data is None:
            return None
        try:
            if hasattr(data, "empty") and data.empty:
                return None
            price = None
            for i in range(len(data)):
                row = data.iloc[i]
                code = str(row.get("code") or "").upper()
                if sym in code:
                    p = self.extract_price(row)
                    if p:
                        price = p
                        break
        except Exception:
            return None
        if price:
            self._stock_cache[sym] = (now, price)
        return price

    def index_quote(self, symbol="SPX"):
        """Index spot for the levels ladder - cached briefly;
        snapshot calls are cheap but the dashboard polls. Tries
        the index code forms, then falls back to the etf proxy
        (spy x 10 tracks the spx closely enough to point at the
        right level) when the index snapshot is refused."""
        now = time.time()
        if self._index_cache is not None and now - self._index_ts < 5:
            return self._index_cache
        sym = symbol.upper()
        proxy = {"SPX": "SPY"}.get(sym)
        price = None
        proxy_price = None
        # this opend build only accepts the US.-prefixed code
        # form - .SPX / SPY.US style forms are rejected and one
        # invalid code fails the WHOLE snapshot request
        groups = [[f"US.{sym}"]]
        if proxy:
            groups.append([f"US.{proxy}"])
        for gi, codes in enumerate(groups):
            try:
                ret, data = self._context().get_market_snapshot(codes)
            except Exception as e:
                # the reconnect path (same as option quotes)
                self._index_error = str(e)
                try:
                    self._ctx.close()
                except Exception:
                    pass
                self._ctx = None
                return None
            if ret != 0:
                # opend puts the failure reason in data
                self._index_error = (
                    f"snapshot ret={ret}: {str(data)[:120]}"
                )
                continue
            if data is None or data.empty:
                self._index_error = "empty snapshot"
                continue
            for i in range(len(data)):
                row = data.iloc[i]
                code = str(row.get("code") or "").upper()
                p = self.extract_price(row)
                if not p:
                    continue
                if proxy and proxy in code:
                    proxy_price = proxy_price or p
                else:
                    price = price or p
            if price:
                self._index_error = None
                break
            if proxy and proxy_price and gi == len(groups) - 1:
                # the index snapshot carries no usable price on
                # this build (no index quote rights) - the etf
                # proxy tracks it: spy x the converter ratio
                price = round(proxy_price * 10.0391, 2)
                self._index_error = None
                self._index_proxy = True
            if gi == 0 and price is None:
                self._index_error = "no usable price in the snapshot"
                self._log_index_snapshot(data)
        if price:
            self._index_last = price
        self._index_cache = self._index_last
        self._index_ts = now
        return self._index_last

    def _log_index_snapshot(self, data, log=print):
        import time as _time

        if _time.time() - getattr(self, "_index_diag_ts", 0) < 300:
            return
        self._index_diag_ts = _time.time()
        try:
            rows = []
            for i in range(len(data)):
                row = data.iloc[i]
                code = str(row.get("code") or "?")
                last = row.get("last_price")
                bid = row.get("bid_price")
                rows.append(f"{code} last={last} bid={bid}")
            log(
                "spx index snapshot returned no usable price - rows: "
                + " | ".join(rows[:6])
            )
        except Exception:
            pass

    def _context(self):
        if self._ctx is None:
            from moomoo import OpenQuoteContext

            self._ctx = OpenQuoteContext(
                host=self.cfg.quotes.moomoo_host,
                port=self.cfg.quotes.moomoo_port,
            )
        return self._ctx

    @staticmethod
    def candidate_codes(pos):
        expiry = str(pos.get("expiry") or "")
        if len(expiry) != 10:
            return []
        yymmdd = expiry[2:4] + expiry[5:7] + expiry[8:10]
        try:
            strike = int(round(float(pos.get("strike") or 0) * 1000))
        except (TypeError, ValueError):
            return []
        right = str(pos.get("right") or "C").upper()
        symbol = str(pos.get("underlying") or "").upper()
        core = f"{symbol}{yymmdd}{right}{strike:08d}"
        # this opend build only accepts the US.-prefixed form -
        # the .US suffix is rejected and one invalid code fails
        # the whole snapshot request
        return [f"US.{core}"]

    @staticmethod
    def extract_price(row):
        def as_float(value):
            try:
                v = float(value)
                return v if v > 0 else None
            except (TypeError, ValueError):
                return None

        get = row.get if hasattr(row, "get") else None
        if get is not None:
            for key in ("bid_price", "last_price"):
                v = as_float(get(key))
                if v is not None:
                    return v
            option = get("option_data")
            if isinstance(option, dict):
                for key in ("bid_price", "last_price"):
                    v = as_float(option.get(key))
                    if v is not None:
                        return v
        return None

    def quote(self, pos):
        codes = self.candidate_codes(pos)
        if not codes:
            return None
        try:
            ret, data = self._context().get_market_snapshot(codes)
        except Exception:
            # OpenD can drop the connection (restart, machine
            # sleep) - drop the cached context so the next
            # poll reconnects instead of staying blind
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None
            return None
        if ret != 0 or data is None:
            return None
        try:
            if hasattr(data, "empty") and data.empty:
                return None
            row = data.iloc[0] if hasattr(data, "iloc") else data[0]
        except Exception:
            return None
        return self.extract_price(row)

    def __call__(self, pos):
        return self.quote(pos)
