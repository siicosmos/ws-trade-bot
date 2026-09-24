from .parser import Alert


def make_quote_provider(cfg, account):
    if not cfg.quotes.enabled:
        print(
            "live option quotes disabled (quotes.enabled=false) - "
            "stop monitor off"
        )
        return None
    provider = (cfg.quotes.provider or "ws").lower()
    if provider == "moomoo":
        try:
            moomoo = MoomooQuoteProvider(cfg)
            moomoo._context()
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
        return [f"US.{core}", f"{core}.US"]

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
