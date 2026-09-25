# Improvement Plan

Current architecture/performance plan. Completed work is
compacted to one line per item - details live in git history.

## Completed (compacted)

- **#1-#13 all landed**: shared margin model, headless-chrome
  UI smoke suite, batched /api/dashboard, sqlite indexes,
  retention pruning, account.py split, static dashboard
  assets, waitress wsgi (https stays on werkzeug), per-thread
  sqlite connections, the trader/ package split, architecture
  + runtime diagrams, history search (merged alert/trade
  stream with alert->trade linking), thread supervision.
- **#14 user access model**: per-user pbkdf2 accounts with
  admin/viewer roles, token-seeded admin, role-gated writes,
  users panel.
- **Reviews**: three passes over #1-#12 plus four deep
  find-fix loop rounds - all findings fixed or documented in
  git history (highlights: PipelineContext refactor, degraded
  batched dashboard, ws_common dedupe, paper p&l math fixes,
  parser stopword fix, mirror partial-holdings credit, the
  windows venv-launcher restart loop, the unbound-local scan,
  the paper pricing expiry-format fix, security hardening).
- **Discord lifecycle**: the reader auto-starts discord,
  re-acquires zombie window handles, closes popups, kills
  hung processes, and navigates server/channel to reach the
  wanted alerts channel (timings configurable).

## Pending plan

### 15. Enforce username+password login

Remove the bare-token fallback: login requires a username and
password pair (the access token stays as the machine
credential for the reader/scripts and seeds the admin on
upgrade). Change the password form to two inputs (new +
confirm) with a mismatch check.

### 16. Roles: admin and regular user

Replace the viewer role with admin / regular. Regular users
log in and see the dashboard; only admins manage users and
settings. Per-user settings records (each user's dashboard
preferences stored per user instead of globally).

### 17. User-related settings move to the database

Reader/dashboard preferences that are per-user (ui state,
notification prefs) move from config.yaml into the users
table; config.yaml keeps only machine and pipeline settings.

### 18. Docs for the user model

Architecture/runtime/db diagrams updated for the users table
and login flow (done for the schema; keep current as the
model evolves).

## Database growth model

Measured in `docs/database_plan.md`: a year of retention
lands at ~5 MB quiet / ~14 MB moderate / ~34 MB heavy - plus
the tiny users table (one row per human). Trivial for sqlite.

## Done / not pursuing

- startup banners, self-healing updater (fetch checks, stale
  index.lock, ignored-runtime-file healing), stale-process
  cleanup (windows venv-launcher-safe), WAL + busy_timeout,
  summary caching, mapped-position caching, history search ui,
  thread supervision (#13), waitress/werkzeug selection, the
  reader's discord lifecycle (auto-start, kill-and-restart,
  popup closing, server/channel navigation)
- reader stays standalone by design (UIA on Windows, no trader
  imports) - duplication of small helpers is accepted
