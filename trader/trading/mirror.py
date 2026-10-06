"""Mirror real Wealthsimple trades into the paper ledger.

Reads the activity feed (get_activities) for each account and
applies the user's own option and stock fills to the paper
positions at their real execution prices - so manually taken
trades show up in the simulation with actual fills, not the
alert's indicative premium.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from .paper import seed_real_accounts

# an estimated live booking older than this is reversed: the
# order never filled, so the ledger must not carry it
PENDING_TTL_SECONDS = 300


class MirrorShim(SimpleNamespace):
    """Just enough alert surface for store.apply_position."""

    def contract_key(self):
        if self.kind != "option":
            return self.underlying
        return (
            f"{self.underlying}-{self.expiry}-{self.strike:g}"
            f"-{self.right}"
        )

    def dedupe_key(self):
        return f"MIRROR|{self.underlying}|{self.expiry}|{self.action}|{self.ts}"


def _option_shim(act):
    strike = act.get("strikePrice")
    try:
        strike = float(strike)
    except (TypeError, ValueError):
        return None
    right = (
        "C" if str(act.get("contractType") or "").upper().startswith(
            "CALL"
        ) else "P"
    )
    return {
        "kind": "option",
        "underlying": str(act.get("assetSymbol") or ""),
        # plain date - the raw value is a full timestamp and
        # would never match the quote map's keys
        "expiry": str(act.get("expiryDate") or "")[:10],
        "strike": strike,
        "right": right,
    }


def _stock_shim(act):
    symbol = str(act.get("assetSymbol") or "").strip()
    if not symbol:
        return None
    return {
        "kind": "stock", "underlying": symbol,
        "expiry": None, "strike": None, "right": None,
    }


def _status_ok(status):
    s = str(status or "").lower()
    return not any(
        word in s for word in ("cancel", "fail", "reject", "revert")
    )


def sweep_pending_orders(cfg, store, ws_account, label,
                         webhook_url=""):
    """Expire estimated live bookings that never filled and
    settle partially-filled ones.

    - no fill: restore the pre-order position exactly (qty and
      basis) so the live ledger only carries real fills.
    - partial fill: keep the filled part booked exactly (the
      ledger position must match what the account actually
      holds) and cancel the stale remainder at the broker.
    - price shock: a partially-filled order whose market price
      ran away from the estimate gets its remainder cancelled
      immediately - the filled part stays as the position for
      future alerts (an ALL OUT later sells what is held).
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=PENDING_TTL_SECONDS)
    ).isoformat(timespec="seconds")
    shock_pct = float(
        getattr(getattr(cfg, "trading", None),
                "partial_fill_cancel_pct", 0) or 0
    )
    try:
        ws = ws_account._client()
    except Exception:
        ws = None
    for row in store.open_pending_orders("live", label):
        mult = 100 if (row["kind"] or "option") == "option" else 1
        filled = int(row.get("filled_qty") or 0)

        # price-shock cancel: a partially-filled order whose
        # market price ran away from the estimate - cancel the
        # remainder now (no ttl wait), keep the filled part as
        # the position for future alerts
        if (
            filled and shock_pct > 0
            and ws is not None and row.get("est_price")
        ):
            current = _contract_market_price(ws_account, row)
            est = float(row["est_price"])
            if current and est > 0:
                move = abs(float(current) - est) / est * 100
            if current and est > 0:
                move = abs(float(current) - est) / est * 100
                if move >= shock_pct:
                    if _shock_cancel(
                        store, ws, row, float(current), move,
                        shock_pct, webhook_url,
                    ):
                        continue

        if (row["placed_ts"] or "") > cutoff:
            continue
        if filled:
            # the filled part is real: correct the estimated
            # booking down to it and let the rest go
            store.correct_fill(
                "live", row["account"], row["contract_key"],
                row["action"], filled_qty=filled,
                actual_price=row.get("filled_price"),
                pre_qty=row["pre_qty"], pre_avg=row["pre_avg"],
                booked_qty=row["qty"], booked_price=row["est_price"],
                mult=mult,
                underlying=row["underlying"], expiry=row["expiry"],
                strike=row["strike"], opt_right=row["opt_right"],
            )
            store.settle_pending_order(row["id"], "partial")
            continue
        store.correct_fill(
            "live", row["account"], row["contract_key"], row["action"],
            filled_qty=0, actual_price=None,
            pre_qty=row["pre_qty"], pre_avg=row["pre_avg"],
            booked_qty=row["qty"], booked_price=row["est_price"],
            mult=mult,
            underlying=row["underlying"], expiry=row["expiry"],
            strike=row["strike"], opt_right=row["opt_right"],
        )
        store.settle_pending_order(row["id"], "expired")


def _contract_market_price(ws_account, row):
    """Current market price for a pending order's contract
    (options via the ws chain, None when unavailable)."""
    if (row.get("kind") or "option") != "option":
        return None
    try:
        from .executor import WealthsimpleExecutor
        from .parser import Alert

        resolver = WealthsimpleExecutor(ws_account.cfg, ws_account)
        ws = ws_account._client()
        sec_id = resolver._resolve_security(ws, row["underlying"])
        if not sec_id:
            return None
        alert = Alert(
            action="SELL",
            ticker=row["underlying"],
            kind="option",
            underlying=row["underlying"],
            expiry=str(row.get("expiry") or "")[:10],
            strike=row.get("strike"),
            right=row.get("opt_right"),
        )
        opt, _ = resolver._resolve_option(ws, sec_id, alert)
        if not opt:
            return None
        quote = opt.get("quote") or {}
        price = (
            quote.get("bid") or quote.get("ask")
            or quote.get("last") or quote.get("price")
        )
        return float(price) if price else None
    except Exception:
        return None


def _shock_cancel(store, ws, row, current_price, move_pct,
                  shock_pct=0, webhook_url=""):
    """Cancel the unfilled remainder of a price-shocked partial
    order and settle the ledger to the filled part."""
    from ..ops.notify import notify_discord

    order_id = str(row.get("order_id") or "")
    if order_id:
        try:
            ws.cancel_order(order_id)
        except Exception as e:
            # already filled / already cancelled at the broker -
            # either way the remainder will not trade against us
            print(f"pending-order cancel: {e}")
    mult = 100 if (row.get("kind") or "option") == "option" else 1
    filled = int(row.get("filled_qty") or 0)
    store.correct_fill(
        "live", row["account"], row["contract_key"], row["action"],
        filled_qty=filled,
        actual_price=row.get("filled_price"),
        pre_qty=row["pre_qty"], pre_avg=row["pre_avg"],
        booked_qty=row["qty"], booked_price=row["est_price"],
        mult=mult,
        underlying=row["underlying"], expiry=row["expiry"],
        strike=row["strike"], opt_right=row["opt_right"],
    )
    store.settle_pending_order(row["id"], "partial_cancelled")
    notify_discord(
        webhook_url,
        f"PARTIAL FILLED - REST CANCELLED: {row['contract_key']}",
        {
            "filled": f"{filled}x of {row['qty']} "
                      f"@ ~{row.get('filled_price'):g}",
            "estimated": f"{row.get('est_price'):g}",
            "market": f"{float(current_price):g}",
            "move": f"{move_pct:.1f}% (limit {shock_pct:g}%)",
            "note": ("the filled part stays as the position - "
                     "future alerts trade against it"),
        },
        ok=False,
    )
    return True


def reconcile_pending_fill(store, label, contract_key, action, qty,
                           price, max_slippage_pct=0, webhook_url=""):
    """Match an actual fill to the oldest open pending live order
    for the same contract and action, then correct the estimated
    booking toward the fill (qty + price), exactly.

    Fills accumulate: a partial fill keeps the order open (with
    its running filled qty / blended price) so later fills of
    the same order still reconcile; the order settles once the
    fills cover its size."""
    from ..ops.notify import notify_discord

    for row in store.open_pending_orders("live", label):
        if (
            row["contract_key"] != contract_key
            or row["action"] != action
        ):
            continue
        mult = 100 if (row["kind"] or "option") == "option" else 1
        total_filled, blended = store.update_pending_fill(
            row["id"], int(qty), price
        )
        store.correct_fill(
            "live", row["account"], contract_key, action,
            filled_qty=total_filled, actual_price=blended,
            pre_qty=row["pre_qty"], pre_avg=row["pre_avg"],
            booked_qty=row["qty"], booked_price=row["est_price"],
            mult=mult,
            underlying=row["underlying"], expiry=row["expiry"],
            strike=row["strike"], opt_right=row["opt_right"],
        )
        est = row["est_price"]
        slip = (
            abs((price or 0) - (est or 0)) / est * 100
            if est else 0.0
        )
        if (
            max_slippage_pct and max_slippage_pct > 0
            and est and price and slip >= float(max_slippage_pct)
        ):
            notify_discord(
                webhook_url,
                f"SLIPPAGE: {action} {contract_key}",
                {
                    "estimated": f"{est:g}",
                    "filled": f"{price:g}",
                    "slippage": f"{slip:.1f}% (limit {max_slippage_pct:g}%)",
                },
                ok=False,
            )
        if total_filled >= int(row["qty"]):
            store.settle_pending_order(row["id"], "filled")
        return True
    return False


def mirror_real_trades(cfg, store, ws_account, ledger=None,
                       webhook_url=""):
    """Apply new real fills to the paper ledger. Returns a list of
    human-readable descriptions for logging."""
    applied = []
    slippage_pct = float(
        getattr(getattr(cfg, "trading", None),
                "max_slippage_pct", 0) or 0
    )
    fx_fn = getattr(ledger, "fx", None)
    try:
        activities_fn = ws_account._client().get_activities
    except Exception:
        return applied
    for label, account_id in ws_account._resolve():
        if not account_id:
            continue
        # the real account's own fills ledger: seeded once from
        # the live holdings (their real average prices) so
        # mirrored sells carry an honest cost basis - the
        # dashboard's real-account today gain reads this ledger
        seed_real_accounts(store, ws_account)
        # expire estimated live bookings that never filled
        sweep_pending_orders(cfg, store, ws_account, label, webhook_url)
        seed = store.meta_get(f"paper_seed:{label}")
        since = store.meta_get(f"mirror:{label}:since")
        if since is None:
            # mirror from the moment the account was seeded
            since = seed or str(time.time())
        try:
            since_ts = float(since)
        except (TypeError, ValueError):
            since_ts = time.time()
        try:
            from datetime import datetime, timezone

            start_date = datetime.fromtimestamp(
                since_ts - 3600, tz=timezone.utc
            ).date().isoformat()
        except Exception:
            start_date = None
        try:
            result = activities_fn(
                account_ids=[account_id],
                types=["buy", "sell"],
                start_date=start_date,
                limit=50,
            )
        except Exception:
            continue
        edges = ((result or {}).get("edges")
                 if isinstance(result, dict) else None) or []
        try:
            seen = set(json.loads(
                store.meta_get(f"mirror:{label}:ids") or "[]"
            ))
        except (TypeError, ValueError):
            seen = set()
        new_ids = []
        latest = since_ts
        for edge in edges:
            act = (edge or {}).get("node") or {}
            cid = act.get("canonicalId")
            if not cid or cid in seen:
                continue
            new_ids.append(cid)
            status = act.get("status")
            try:
                qty = abs(float(act.get("assetQuantity") or 0))
            except (TypeError, ValueError):
                qty = 0.0
            if not qty or not _status_ok(status):
                continue
            action = str(act.get("type") or "").upper()
            if action not in ("BUY", "SELL"):
                continue
            info = _option_shim(act) or _stock_shim(act)
            if info is None:
                continue
            amount = act.get("amount")
            try:
                amount = abs(float(amount or 0))
            except (TypeError, ValueError):
                amount = 0.0
            # amount is the executed total; per-unit premium for
            # options divides by the contract multiplier
            mult = 100 if info["kind"] == "option" else 1
            premium = amount / (qty * mult) if qty else None
            occurred = str(act.get("occurredAt") or "")
            shim = MirrorShim(
                action=action, premium=premium, entry=premium,
                stop_loss=None, take_profit=None,
                ts=occurred or str(time.time()), **info,
            )
            fx = float(fx_fn()) if callable(fx_fn) else 1.0
            mult = 100 if info["kind"] == "option" else 1
            delta = int(qty) if action == "BUY" else -int(qty)
            # the real account's own ledger books every fill at
            # its actual price - the real account card's today
            # gain reads this, so a real loss shows negative even
            # when the paper simulation gained
            real_held = store.get_position(
                "real", shim.contract_key(), label
            )
            if action == "BUY" or real_held >= 1:
                store.apply_position(
                    "real", shim, delta, premium=premium, account=label
                )
            # reconcile the live ledger's estimated booking
            # against this actual fill (no-op when no pending
            # live order matches)
            # reconcile the live ledger's estimated booking
            # against this actual fill (no-op when no pending
            # live order matches); a fill that slips past the
            # configured % from the estimate posts a notice
            reconcile_pending_fill(
                store, label, shim.contract_key(), action, int(qty),
                premium, max_slippage_pct=slippage_pct,
                webhook_url=webhook_url,
            )
            if action == "SELL":
                held = store.get_position(
                    "paper", shim.contract_key(), label
                )
                if held < 1:
                    # the paper ledger does not hold this fill - the
                    # real account traded something the simulation
                    # missed or already closed (the real ledger
                    # booked it above)
                    continue
                # sell only what the ledger actually holds: a partial
                # holding must not credit the full real fill's
                # proceeds (the position clamps at zero either way)
                qty = min(int(qty), int(held))
            delta = int(qty) if action == "BUY" else -int(qty)
            store.apply_position(
                "paper", shim, delta, premium=premium, account=label
            )
            # per-unit basis keeps the paper equity consistent with
            # what the ledger actually traded
            proceeds = qty * (premium or 0.0) * mult * (
                fx if info["kind"] == "option" else 1.0
            )
            if action == "BUY":
                store.adjust_paper_equity(-proceeds, label)
            else:
                store.adjust_paper_equity(proceeds, label)
            store.record_trade(
                "paper", action, shim.underlying, int(qty), premium,
                shim, "executed",
                f"mirrored real fill: {amount:g} "
                f"{act.get('currency') or ''}".rstrip(),
            )
            applied.append(
                f"{label}: {action} {int(qty)} "
                f"{shim.contract_key()} @ {premium:g}"
            )
        if new_ids:
            seen.update(new_ids)
            store.meta_set(
                f"mirror:{label}:ids",
                json.dumps(list(seen)[-200:]),
            )
        store.meta_set(f"mirror:{label}:since", time.time())
    return applied


def start_mirror_thread(cfg, store, ws_account, ledger,
                        interval_seconds=60, webhook_url=""):
    """Mirror real trades on a daemon timer."""

    def run():
        while True:
            time.sleep(max(15, int(interval_seconds)))
            # daily history prune rides this loop too - a quiet
            # bot must still age rows out (plan finding)
            try:
                store.maybe_prune()
            except Exception:
                pass
            # live mode: the pass is unconditional - fill
            # reconciliation cannot depend on the paper-mirror
            # toggle; paper mode: only when mirroring is on
            if getattr(cfg.trading, "mode", "") != "live" and (
                not getattr(cfg, "paper", None) or not cfg.paper.mirror
            ):
                continue
            try:
                applied = mirror_real_trades(
                    cfg, store, ws_account, ledger,
                    webhook_url=webhook_url,
                )
            except Exception as e:
                print(f"trade mirror error: {e}")
                continue
            for line in applied:
                print(f"trade mirror: {line}")

    from ..ops.supervise import supervised

    thread, _ = supervised("mirror", run, webhook_url)
    return thread
