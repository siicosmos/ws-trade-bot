# Database Plan

Moved into `improvement_plan.md` (finished items - database).
This file keeps the deep-dive reference tables for sizing and
maintenance. The schema lives in `db_schema.dot` (rendered
png/svg).

## Storage model

- **WAL** (`journal_mode=WAL`, `synchronous=NORMAL`): readers
  run lock-free alongside the writer; `busy_timeout=5000`
  absorbs contention
- **Per-thread connections**: the pipeline's request workers
  and background threads each get their own connection
- **Retention**: `Store.maybe_prune()` runs once per calendar
  day and drops `signals` and `trades` rows older than
  `trading.history_retention_days` (default **365**, `0` =
  keep forever). Positions, paper ledgers, seeds, loss-streak
  and mirror state are never pruned
- **Display vs history**: the dashboard's batched endpoint
  sends only the latest 50 signals and 50 trades; older rows
  are kept for the history search

## Row sizes (measured with real alert shapes, indexes included)

- ~**280 B** per `signals` row
- ~**280 B** per `trades` row (incl. index entries)
- ~**850 B per actionable alert** (signal + notify row + paper
  row), ~**280 B per ignored chatter message**

One year of history:

| volume | chatter/day | alerts/day | steady-state size |
|---|---|---|---|
| quiet | 20 | 10 | ~5 MB |
| moderate | 60 | 25 | ~14 MB |
| heavy | 150 | 60 | ~34 MB |

`positions`, `meta` and `users` are bounded small - kilobytes.

## Maintenance guidance

- **No action needed at these sizes** - orders of magnitude
  below sqlite's comfort zone
- **The file plateaus, it does not shrink**: retention deletes
  free pages internally and they are reused. A manual
  `VACUUM` compacts it; no reason to automate one here
- **`trades.db-wal` is transient**: auto-checkpointing bounds
  it; it disappears on clean shutdown
- **Backups**: copying `trades.db` while the pipeline runs can
  miss WAL contents - use `sqlite3 trades.db ".backup backup.db"`
  (or stop the pipeline) for consistent copies
- **Clean slate**: `scripts/clean_start.py` wipes signals and
  trades (keeps paper state and config)
- **Users table**: one row per human (pbkdf2-hashed password,
  role, login timestamps) - a few KB; passwords never leave
  the hash, listings omit it
