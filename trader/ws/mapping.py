"""Position mapping: raw Wealthsimple GraphQL nodes into the
dashboard's option strategy rows and stock holdings.

Split out of trader/account.py (plan #6) - pure logic over the
fetched nodes plus the multi-leg grouping into verticals,
butterflies, condors and iron flies. map_options takes the
usd-cad quote callable so it has no transport dependency."""

from ..trading.strategies import classify_legs
from .ws_common import _amount, _amount_opt, _quote_price


def map_stocks(raw):
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


def map_options(raw, usd_cad_quote):
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
                leg_fx = usd_cad_quote()

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
            fx = usd_cad_quote()
        out[label] = {
            "positions": rows,
            "fx": fx,
            "usd_cash": usd_cash,
        }
    return out
