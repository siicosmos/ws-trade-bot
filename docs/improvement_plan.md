# Improvement Plan

Architecture, database and access-model plan for the bot.
Unfinished work first; everything landed is compacted at the
end - details live in git history.

## Planned items

### 1. Enforce username+password login

Remove the bare-token fallback: login requires a username and
password pair. The access token stays as the machine
credential for the reader and scripts and seeds the admin on
upgrade - but can no longer open a browser session on its own.
The change-password form gets two inputs (new + confirm) with
a mismatch check.

### 2. Roles: admin and regular user

Replace the viewer role with admin / regular. Regular users
log in and see the dashboard; only admins manage users and
settings. Per-user settings records: each user's dashboard
preferences are stored per user instead of globally.

### 3. User-related settings move to the database

Reader/dashboard preferences that are per-user (ui state,
notification prefs) move from config.yaml into the users
table; config.yaml keeps only machine and pipeline settings.

### 4. Docs for the user model

Keep architecture/runtime/db diagrams current as the login
and settings model evolves (schema and architecture dot
already cover the users table; re-render on changes).

## Finished items

### Database (trades.db, sqlite WAL)

Row sizes measured with real alert shapes, indexes included:
~**280 B** per `signals` row, ~**280 B** per `trades` row -
~**850 B per actionable alert** (signal + notify row + paper
row), ~**280 B per ignored chatter message**. One year of
history:

| volume | chatter/day | alerts/day | steady-state size |
|---|---|---|---|
| quiet | 20 | 10 | ~5 MB |
| moderate | 60 | 25 | ~14 MB |
| heavy | 150 | 60 | ~34 MB |

`positions`, `meta` and `users` are bounded small. Notes:

- Per-thread connections (WAL + busy_timeout), writes
  serialized, reads lock-free
- Indexes: trades(mode, ts), trades(dedupe_key, ts),
  trades(message_key), positions covered by its PK -
  EXPLAIN-verified in tests
- Retention: daily prune of signals/trades past
  history_retention_days (365 default), riding both the write
  paths and the always-on health watchdog; the file plateaus
  by design (freed pages are reused), no automated VACUUM
- Backups: `sqlite3 trades.db ".backup backup.db"` while
  running (a raw copy can miss WAL contents); clean slate via
  `scripts/clean_start.py`
- History search over the retained rows: merged alert/trade
  stream, the alert->trade link via message_key (indexed),
  LIKE-escaped free text with date ranges
- Users table: one row per human (pbkdf2-hashed password,
  role, login timestamps) - a few KB at this scale; passwords
  never leave the hash, listings omit it

### Application

- #1-#13 all landed: shared margin model, headless-chrome UI
  smoke suite (23 checks), batched /api/dashboard, account.py
  split into ws/trading/web/ops packages, static dashboard
  assets, waitress wsgi (https stays on werkzeug), thread
  supervision, architecture/runtime diagrams
- #14 user access model: per-user pbkdf2 accounts, admin
  gating of writes, users panel, token-seeded admin
- Reader discord lifecycle: auto-start (configurable timing),
  kill-and-restart of hung processes, popup closing, zombie
  window re-acquisition, server/channel navigation
- Reviews: three passes over #1-#12 plus four deep find-fix
  rounds - all findings fixed or documented (highlights:
  PipelineContext refactor, degraded batched dashboard,
  ws_common dedupe, paper p&l math, parser stopwords, mirror
  partial-holdings credit, the windows venv-launcher restart
  loop, the unbound-local scan, paper pricing expiry fix,
  security hardening)

### Not pursuing

- reader stays standalone by design (UIA on Windows, no
  trader imports) - duplication of small helpers is accepted
- FTS5 for history search: LIKE is fine at measured sizes;
  revisit only if the corpus grows orders of magnitude
- automated VACUUM: the file plateaus by design, retention
  reuses freed pages
