# Live-trading readiness plan

Hardening work before and around real money. Landed items are
compacted at the end; the planned items stay here with their
technical detail. See also `docs/improvement_plan.md`.

## Planned items

### 1. Correlation-aware open risk (P1)

Three same-direction SPX 0dte calls count as three positions
against `max_open_risk_pct` but are one bet x3. The open-risk
cap should see clusters.

- `store.open_risk(mode, account)` gains a clustered variant:
  group open option positions by (underlying, expiry, right)
  and sum qty x premium x 100 per cluster; report both the flat
  total and the largest cluster.
- Executor `_at_open_risk_cap` and the notify sizing warnings
  take a `cluster_cap_pct` (new setting, default 50 = a single
  cluster may never exceed half the account's open-risk cap).
  A buy into an already-capped cluster is skipped even when the
  global cap has room.
- Dashboard: the open-risk sub line flags "largest cluster = x%
  of cap" when one cluster dominates.
- Verify: test that two SPX calls + one SPY call trip the
  cluster cap while staying under the global cap.

### 2. Slippage guard in live mode (P1)

Live sells price off the quote bid at signal time; a fast tape
means the fill lands below. The mirror now reconciles actual
fills against the pending orders, so the check is cheap:

- When `reconcile_pending_fill` settles an order, compare the
  estimated price vs the fill price; if the difference exceeds
  `max_slippage_pct` (new setting, default 2%), post a discord
  notice with both prices. Data only at first - no auto-pause -
  until the first weeks of live data say otherwise.

### 3. Reader resilience review (P2)

The pipeline's single point of failure is the Windows UIA
scraper on one Discord window. Heartbeat, watchdog, restart and
the raw-alert webhook already exist. Remaining: a daily human
check of the dashboard reader line ("watching: ..."), and
optionally a second allowed channel as fallback in
`reader.channels`. No code planned unless the heartbeat shows
missed windows.

## Landed

### Pending-order fill reconciliation (was P0 #2)

The live executor no longer trusts its own estimates:

- `pending_orders` table (trader/store.py): every live order
  records the contract, action, qty, estimated price and the
  pre-order position snapshot (pre_qty, pre_avg) at placement.
- The mirror pass sweeps orders older than
  `PENDING_TTL_SECONDS` (300s) with no fill and restores the
  pre-order position exactly - qty and average premium.
- Actual fills (from the ws activity feed, deduped by
  canonicalId) reconcile against the oldest matching open
  order: qty becomes pre +/- filled, sell realized is the truth
  (fill vs the real basis, minus what the estimate booked),
  buy basis re-blends the actual price. `realized_today` moves
  by the same correction, so the lotto budget stays honest.
- The mirror thread runs unconditionally in live mode
  (run.py) - reconciliation cannot depend on the paper-mirror
  toggle.
- Tests: tests/test_mirror.py (full fill, partial fill, sweep).

### Hard daily-loss circuit breaker (was P1 #4)

`max_daily_loss_pct` (trading config, settings UI "global risk
cap" sub-section): once today's realized pnl sinks below this
% of account value, `RiskEngine.evaluate` blocks every new BUY
(options and stocks). Exits stay allowed. 0 = off (default).

### Live stock buys enforce the open-risk cap (was P0 #3)

`_execute_stock`'s live buy path now runs
`_at_open_risk_cap` like the paper path - and the bare
`account.value(label)` reference (a latent NameError when a
stock tier was configured) is fixed to `self.account`.

### Paper expectancy report (was P0 #1)

`scripts/expectancy.py [--db trades.db] [--mode paper]`:
closes round trips from the positions ledger and reports per
account + size tier - trade count, win rate, avg win, avg loss,
expectancy per trade, realized total - plus a best-effort
slippage section (alert premium vs mirrored real fill for the
same contract/action/day). A tier negative over >= 30 trades
prints a `contracts_max: 0` recommendation.

### `place_stop_loss` removed (was P2 #7)

The setting was inert - no executor path read it - while the
settings tooltip implied resting stop orders. Removed from the
config, the settings schema and the UI. Exits come from the
quote-driven StopMonitor; real resting stops only get
re-introduced if its `stop_check_seconds` latency ever proves
costly.

### Real-fills ledger for the real account card

The mirror books every real fill (bot + manual) at its actual
price into a per-account `mode="real"` ledger, seeded once from
the live holdings. The dashboard's real-account "today" line
reads it - a real loss shows negative and never repeats the
paper simulation's number.

## Go-live checklist (process, not code)

1. Paper expectancy positive over >= 30 trades per tier used
   (`scripts/expectancy.py`).
2. Live config: smallest tier only (`contracts_max` clamped),
   lowest risk %, one account enabled, `max_open_risk_pct`
   tightened, `max_daily_loss_pct` set.
3. First week: notify-style observation with live-sized paper -
   compare the real card's fills vs the paper card (slippage
   check) before any real order.
4. Margin account stays manual until the RRSP results prove the
   source out.
