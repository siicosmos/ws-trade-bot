# Live-trading readiness plan

Hardening work before and around real money. Landed items are
compacted at the end; the planned items stay here with their
technical detail. See also `docs/improvement_plan.md`.

## Planned items

None - the original P0/P1/P2 list is fully landed below. The
one review item without code:

### 3. Reader resilience review (P2) - reviewed, no code

The pipeline's single point of failure is the Windows UIA
scraper on one Discord window. Reviewed with the current
guards in place: heartbeat + dashboard reader line, watchdog,
auto-restart, the raw-alert webhook and the multi-channel
allowlist already cover the failure modes the review was
worried about. The remaining item is process, not code: a
daily glance at the dashboard reader line ("watching: ...").
A second allowed channel as fallback stays an option in
`reader.channels` (it is an allowlist already) - no code until
the heartbeat shows missed windows.

## Landed

### Correlation-aware open risk (was planned #1)

`cluster_cap_pct` (trading config, settings UI "global risk
cap" sub-section, default 50 = a single cluster may never
exceed half the account value):

- `store.open_risk_clusters(mode, account)` groups open option
  positions by (underlying, expiry, right) and sums
  qty x premium x 100 per cluster, largest first.
- A buy into an already-capped cluster is skipped even when
  the global cap has room - in paper, live (option buys) and
  the notify sizing warnings ("cluster cap reached: ...").
- Dashboard: the real card's open-risk sub line flags the
  largest cluster once it nears the cluster cap.
- Verify: tests/test_live_executor.py
  `test_live_option_buy_cluster_cap` - two SPX call buys fill
  the cluster, the third is skipped, a SPY call still trades.

### Slippage guard in live mode (was planned #2)

`max_slippage_pct` (default 2%): when the mirror reconciles an
actual fill against its pending order, a fill landing beyond
this % from the order's estimated price posts a discord notice
with both prices. Data only - no auto-pause.

### Partial-fill price-shock cancel (extra)

Partially-filled live orders are handled properly now:

- Fills accumulate on the pending order (running filled qty +
  blended price) - a partial fill keeps the order open so the
  next fill of the remainder still reconciles (it used to
  settle "filled" on the first fill and orphan the rest).
- The 300s sweep keeps the FILLED part of a partial order
  booked exactly (previously a partial fill past the ttl was
  reverted entirely, erasing real holdings from the ledger).
- `partial_fill_cancel_pct` (default 10%): a partially-filled
  order whose market price ran this % away from the estimate
  gets its remainder cancelled at the broker immediately
  (`cancel_order`); the filled part stays as the position for
  future alerts (an ALL OUT later sells what is held). Discord
  notice with fill/estimate/market.
- The mirror pass runs unconditionally in live mode (the
  paper-mirror toggle no longer gates reconciliation).

### Per-position sell guards: take-profit + trailing (extra #2)

"ALL OUT" is not guaranteed to arrive, so each position can
carry its own guards, set beside the position in the dashboard
(admin, options only) - one modal with both fields:

- `positions.tp_gain_pct` / `positions.trail_pct` (store
  columns) + `set_position_tp()` / `set_position_trail()`; the
  guards ride the position row through the dashboard.
- Take-profit: the stop monitor sells the whole remaining
  position when the bid reaches entry x (1 + tp%) - `[TP]`
  trades, running even with the global stops off.
- Trailing: the position's own `trail_pct` overrides the global
  trailing stop - including enabling it while the global
  trailing is off, and disabling it (0) where the global one is
  on. Fires like a stop once the bid falls the % off the
  position's peak bid.
- `POST /api/position-tp` (admin) sets/clears both per
  (mode, account, contract_key); the tp button beside every
  paper + tracked position row opens both fields, with
  "TP x%" / "TS x%" chips on the row.

### Live-executor test suite + kill switch (safety pass)

Before any live order, the code that touches real money is now
exercised as thoroughly as the paper paths:

- `tests/test_live_executor.py` drives every WealthsimpleExecutor
  path against a fake ws client (chain resolution, quotes, order
  placement, position queries): option buy sizing + booking +
  pending-order snapshot, sell flattening + realized pnl,
  open-risk-cap skip, lotto-budget gate, stock tier sizing +
  open-risk cap, sell-only-if-held, rejected-order cleanup
  (nothing booked, nothing pending), stop-monitor exits riding
  the live executor, and the kill switch.
- `trading_paused` (settings "trading paused (kill switch)"):
  a runtime toggle in the automation section - flipping it
  blocks every new BUY immediately, no restart needed. Exits
  (alert sells, stops, b2e) stay allowed so open positions keep
  their protection while entries are halted.
- This pass also fixed an argument-order bug the new tests
  caught: the four `position_state(mode, account, contract_key)`
  call sites in the live executor passed contract/account
  swapped, so pre-order snapshots always read empty.

### Pending-order fill reconciliation (was P0 #2)

The live executor no longer trusts its own estimates:

- `pending_orders` table (trader/store.py): every live order
  records the contract, action, qty, estimated price and the
  pre-order position snapshot (pre_qty, pre_avg) at placement.
- The mirror pass sweeps orders older than
  `PENDING_TTL_SECONDS` (300s) with no fill and restores the
  pre-order position exactly - qty and average premium
  (partially-filled orders are kept for their filled part -
  see the partial-fill section above).
- Actual fills (from the ws activity feed, deduped by
  canonicalId) reconcile against the oldest matching open
  order: qty becomes pre +/- filled, sell realized is the truth
  (fill vs the real basis, minus what the estimate booked),
  buy basis re-blends the actual price. `realized_today` moves
  by the same correction, so the lotto budget stays honest.
- The mirror thread runs unconditionally in live mode
  (run.py) - reconciliation cannot depend on the paper-mirror
  toggle.
- Tests: tests/test_mirror.py (full fill, partial fill,
  accumulation, sweep, price-shock cancel).

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
   tightened plus `cluster_cap_pct` for same-bet clusters,
   `max_daily_loss_pct` set. Fill handling: `partial_fill_cancel_pct`
   and `max_slippage_pct` at their defaults (10% / 2%) until
   the first weeks of fills say otherwise.
3. First week: notify-style observation with live-sized paper -
   compare the real card's fills vs the paper card (slippage
   check) before any real order.
4. Know the brake: the settings kill switch ("trading paused")
   halts all new buys in one click; the daily-loss breaker caps
   the day automatically.
5. Open positions keep their protection without alerts: the
   stop monitor's global stop/trailing plus the per-position
   tp/trailing guards (set beside each position) and b2e cover
   an ALL OUT that never arrives.
6. Margin account stays manual until the RRSP results prove the
   source out.
