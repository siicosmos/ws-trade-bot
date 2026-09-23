# Database Plan

`trades.db` (SQLite, WAL mode) holds everything the bot
records: parsed signals, the trade log, paper positions and
runtime state. This document is the sizing and maintenance
plan. The schema lives in `db_schema.dot` (rendered png/svg).

## Storage model

- **WAL** (`journal_mode=WAL`, `synchronous=NORMAL`): readers
  run lock-free alongside the writer; `busy_timeout=5000`
  absorbs contention
- **Per-thread connections**: the pipeline's request workers
  and background threads each get their own connection
- **Retention**: `Store.maybe_prune()` runs once per calendar
  day (called from the record paths) and drops `signals` and
  `trades` rows older than `trading.history_retention_days`
  (default **365**, `0` = keep forever). Positions, paper
  ledgers, seeds, loss-streak and mirror state are never
  pruned
- **Display vs history**: the dashboard's batched endpoint
  sends only the latest 50 signals and 50 trades; older rows
  are kept for the future search UI (improvement plan #12)

## Growth model (measured, not estimated)

Representative rows in a scratch database with real alert
shapes, both trade rows per actionable alert, indexes
included:

- ~**280 B** per `signals` row (message_key, timestamps,
  alert text up to 2 KB cap in practice ~100-150 chars)
- ~**280 B** per `trades` row (incl. `idx_trades_mode_ts` and
  `idx_trades_dedupe` entries; the paper row carries the
  sizing detail text)
- therefore ~**850 B per actionable alert** (signal + notify
  row + paper row) and ~**280 B per ignored chatter message**

One year of history:

| volume | chatter/day | alerts/day | steady-state size |
|---|---|---|---|
| quiet | 20 | 10 | ~5 MB |
| moderate | 60 | 25 | ~14 MB |
| heavy | 150 | 60 | ~34 MB |

`positions` and `meta` are bounded by the number of accounts
and open positions - kilobytes.

## Maintenance guidance

- **No action needed at these sizes.** SQLite remains
  comfortable orders of magnitude above this
- **The file plateaus, it does not shrink**: retention deletes
  free pages internally and they are reused by new rows. A
  manual `VACUUM` compacts it, but there is no reason to
  automate one at this scale
- **`trades.db-wal` is transient**: auto-checkpointing bounds
  it; it disappears on clean shutdown
- **Backups**: copying `trades.db` while the pipeline runs can
  miss WAL contents - use `sqlite3 trades.db ".backup backup.db"`
  (or stop the pipeline) for consistent copies
- **Clean slate**: `scripts/clean_start.py` wipes signals and
  trades (keeps paper state and config); the daily prune makes
  it unnecessary for size reasons

## Search UI notes (plan #12)

- Use SQLite **FTS5** over the signals/trades text for the
  free-text search endpoint - instant results without LIKE
  scans (LIKE would still be fine at these sizes, but FTS
  keeps the endpoint simple)
- `idx_trades_mode_ts` already backs date-ranged scans
- `received_ts` is NULL on signals recorded before that column
  existed - treat it as unknown, do not sort on it
