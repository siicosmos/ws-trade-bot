"""Paper trading: the seeded ledger and per-account seeding.

Split out of trader/account.py (plan #6) - this module owns the
simulated cash + holdings state and the seeding that mirrors
live long positions. Depends only on ws_common helpers and the
store."""

import time

from consumer.ws.ws_common import (
    _amount_opt,
    _quote_price,
    account_label,
    effective_accounts,
)


def _position_key(pos):
    """Canonical quote-map key for a stored position row.

    Older seeds stored the raw graphql expiry timestamp in the
    contract key; the live quotes are keyed by the plain date
    (see _quotes) - normalize on lookup so the paper price
    always matches the real account's quote."""
    expiry = str(pos.get("expiry") or "")[:10]
    strike = pos.get("strike")
    right = pos.get("right")
    if not expiry or strike is None or not right:
        return pos["contract_key"]
    try:
        return (
            f"{pos['underlying']}-{expiry}"
            f"-{float(strike):g}-{right}"
        )
    except (TypeError, ValueError):
        return pos["contract_key"]


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
        self._chain_backoff_until = 0.0

    def _moomoo_quotes(self, quotes):
        """Mark paper option positions from the moomoo feed:
        paper-only trades (notify mode never executed them) have
        no live-account counterpart, so the ws positions path
        can't price them - one batch snapshot covers every
        paper contract."""
        from consumer.trading.quotes import ACTIVE_QUOTE_PROVIDER

        provider = ACTIVE_QUOTE_PROVIDER
        if provider is None or not hasattr(provider, "candidate_codes"):
            return quotes
        codes = []
        by_code = []
        try:
            for pos in self.store.list_positions("paper"):
                if not pos.get("right"):
                    continue
                key = _position_key(pos)
                if key in quotes:
                    continue   # already priced from ws
                probe = dict(pos, expiry=str(pos.get("expiry") or "")[:10])
                for c in provider.candidate_codes(probe):
                    codes.append(c)
                    by_code.append((key, c.upper()))
            if not codes:
                return quotes
            ret, data = provider._context().get_market_snapshot(codes)
            if ret != 0 or data is None or data.empty:
                return quotes
            for i in range(len(data)):
                row = data.iloc[i]
                p = type(provider).extract_price(row)
                if not p:
                    continue
                code = str(row.get("code") or "").upper()
                for key, c in by_code:
                    if c == code and key not in quotes:
                        quotes[key] = {"price": p, "usd": True}
        except Exception:
            pass
        return quotes

    def _ws_chain_quotes(self, quotes):
        """Paper-only contracts priced from the ws option chains.

        Without moomoo there is no feed to ride for paper-only
        trades (notify mode never executed them, so no live
        node carries their quote) - resolve each contract the
        same way the executor does and take its bid. This keeps
        the paper price consistent with what the real account
        card shows for the same contract."""
        if self.ws_account is None:
            return quotes
        pending = []
        for pos in self.store.list_positions("paper"):
            if not pos.get("right"):
                continue
            key = _position_key(pos)
            if key in quotes:
                continue
            if len(str(pos.get("expiry") or "")) != 10:
                continue
            pending.append((key, pos))
        if not pending:
            return quotes
        if time.time() < self._chain_backoff_until:
            return quotes
        try:
            from consumer.trading.executor import WealthsimpleExecutor

            resolver = WealthsimpleExecutor(self.cfg, self.ws_account)
            # the account's own client - a fresh WealthsimpleV2
            # would be unauthenticated
            ws = self.ws_account._client()
        except Exception:
            # ws down - back off so the dashboard payload is not
            # stalling on network timeouts every refresh
            self._chain_backoff_until = time.time() + 600
            return quotes
        added = False
        # bound the walk: each unpriced contract costs two ws
        # round-trips (security + option resolve, 15s timeout
        # each) - an unbounded loop over many positions would
        # stall the stop monitor / dashboard refresh for minutes.
        # the window rotates so every contract eventually gets a
        # turn across successive refreshes
        cursor = (
            getattr(self, "_chain_cursor", 0) % len(pending)
            if pending else 0
        )
        batch = [
            pending[(cursor + i) % len(pending)]
            for i in range(min(6, len(pending)))
        ]
        self._chain_cursor = cursor + 6
        for key, pos in batch:
            try:
                sec_id = resolver._resolve_security(
                    ws, pos["underlying"]
                )
                if not sec_id:
                    continue
                from core.parser import Alert

                alert = Alert(
                    action="SELL",
                    ticker=pos["underlying"],
                    kind="option",
                    underlying=pos["underlying"],
                    expiry=pos["expiry"],
                    strike=pos.get("strike"),
                    right=pos.get("right"),
                )
                opt, _ = resolver._resolve_option(ws, sec_id, alert)
                if not opt:
                    continue
                # chain nodes carry the quote under quoteV2 - the
                # same field the live position card prices from,
                # so paper and real agree on the same contract
                quote = opt.get("quoteV2") or opt.get("quote") or {}
                price = (
                    quote.get("price")
                    or quote.get("last")
                    or quote.get("bid")
                    or quote.get("ask")
                )
                if price:
                    quotes.setdefault(
                        key, {"price": float(price), "usd": True}
                    )
                    added = True
            except Exception:
                continue
        if not added:
            self._chain_backoff_until = time.time() + 300
        return quotes

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
        try:
            quotes = self._moomoo_quotes(quotes)
        except Exception:
            pass
        try:
            quotes = self._ws_chain_quotes(quotes)
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
        # the adjust editor's usd cash pool (converted like the
        # usd holdings)
        cash_usd = self.store.paper_cash_usd(label) or 0.0
        total = cash + cash_usd * fx
        for pos in self.store.list_positions("paper", label):
            quote = quotes.get(_position_key(pos)) or quotes.get(
                pos["contract_key"]
            )
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
            quote = quotes.get(_position_key(pos)) or quotes.get(
                pos["contract_key"]
            )
            is_option = pos.get("right") not in (None, "", "?")
            if quote is None:
                price = pos.get("avg_premium") or 0.0
                usd = is_option
            else:
                price = quote["price"]
                usd = quote.get("usd", is_option)
            # usd rows quote in usd and book in cad - the ui shows
            # the usd amounts with the cad value in brackets, so
            # value/cost stay in the quote currency here
            mult = 100 if is_option else 1
            value = (pos["qty"] or 0) * price * mult
            cost = (
                (pos["qty"] or 0) * (pos.get("avg_premium") or 0.0)
                * mult
            )
            pnl = (
                round((value / cost - 1) * 100, 1)
                if cost else None
            )
            rows.append(
                {
                    "contract_key": pos["contract_key"],
                    "underlying": pos.get("underlying"),
                    "expiry": pos.get("expiry"),
                    "strike": pos.get("strike"),
                    "right": pos.get("right"),
                    "kind": "option" if is_option else "stock",
                    "usd": bool(usd),
                    "qty": pos["qty"],
                    "avg": round(pos.get("avg_premium") or 0.0, 4),
                    # realtime per-unit price and the (static)
                    # total cost basis for the holdings table,
                    # plus the cad conversion for the brackets
                    "price": round(price, 4),
                    "value": round(value, 2),
                    "cost": round(cost, 2),
                    "value_cad": round(
                        value * (fx if usd else 1.0), 2
                    ),
                    "cost_cad": round(
                        cost * (fx if usd else 1.0), 2
                    ),
                    "pnl": pnl,
                    "pnl_dollars": round(value - cost, 2),
                    # per-position guards (set in the ui, fired
                    # by the stop monitor)
                    "tp_gain_pct": pos.get("tp_gain_pct"),
                    "trail_pct": pos.get("trail_pct"),
                }
            )
        return rows

    def values(self) -> dict:
        return {
            account_label(a): self.value(account_label(a))
            for a in effective_accounts(self.cfg)
        }


def _seed_longs(nodes):
    """Long option + stock rows (key, underlying, expiry, strike,
    right, qty, avg) and their combined cad value, extracted from
    the raw ws position nodes. Shared by the paper seed and the
    real-fills ledger seed."""
    longs = []
    positions_value = 0.0
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
            # keys and stored expiries use the plain date -
            # the raw graphql value is a full timestamp,
            # which never matched the quote map and froze
            # the paper price at cost basis
            expiry_date = str(od.get("expiryDate") or "")[:10]
            if not short and qty > 0:
                positions_value += abs(mv) * conv
                longs.append(
                    (
                        f"{underlying}-{expiry_date}"
                        f"-{strike:g}-{right}",
                        underlying, expiry_date,
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
    return longs, positions_value


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
        longs, positions_value = _seed_longs(raw.get(label) or [])
        cash = round(value - positions_value, 2)
        store.set_paper_equity(cash, label)
        for row in longs:
            store.seed_position("paper", label, *row)
        store.meta_set(f"paper_seed:{label}", time.time())
        store.meta_set(f"paper_initial:{label}", value)
        seeded.append(label)
    return seeded


def seed_real_accounts(store, ws_account):
    """Seed each real account's fills ledger (mode="real") from
    its live holdings, once.

    The mirror books every real fill into this ledger at its
    actual price - the real account card's today gain reads it.
    Seeding the live long positions first gives sells of
    pre-mirroring positions an honest cost basis."""
    try:
        raw = ws_account._positions_raw() or {}
    except Exception:
        return
    for label, account_id in ws_account._resolve():
        if not account_id:
            continue
        if store.meta_get(f"real_seed:{label}"):
            continue
        longs, _value = _seed_longs(raw.get(label) or [])
        for row in longs:
            store.seed_position("real", label, *row)
        store.meta_set(f"real_seed:{label}", time.time())
