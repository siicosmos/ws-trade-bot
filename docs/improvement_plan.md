# Improvement Plan

Architecture and performance review of the codebase, kept
current as items land. Each pending item is independent - pick
them off as time allows.

## 1. Shared margin model - DONE

`trader/margin.py` (Holding / compute_requirement /
resolve_rate) is the single source of truth. Both the real-card
computation in `_account_summaries` and `_paper_card_metrics`
build normalized Holdings and call in; rate resolution
differences are injected resolvers. Breakdown line formats are
unified across both cards.

## 2. Headless-Chrome UI smoke test - DONE

`tests/scripts/ui_test.py` renders the real dashboard in
headless chrome (`--dump-dom`) with a stubbed `api()`, 17
checks covering the interactions that historically broke while
unit tests passed: paper currency flip, eye masking of
cash/seeded, margin breakdown open/mask/arrow states, holdings
arrow on an empty ledger, negative-zero rendering. Runs as the
final phase of `tests/scripts/e2e_test.py`, soft-skips without
a browser on PATH (`CHROME_BIN` overrides).

## 3. Batched dashboard endpoint - DONE

`/api/dashboard` composes summary, paper positions, positions,
signals, trades, settings and update status in one response
(payload builders shared with the individual routes, which
remain available). The client's 5s poll went from seven
requests to one; every button action refreshes through the
same round trip; loaders are pure render functions. A summary
failure degrades to its error marker - the rest of the
payload still renders.

## 4. SQLite indexes - DONE

`trades(mode, ts)` backs `trades_today` / `loss_streak`, and
`trades(dedupe_key, ts)` backs the dedupe lookup (EXPLAIN
QUERY PLAN verified in tests). The `positions` PK already
covers its `mode + account` lookups, so no extra index there.

## 5. Retention for signals/trades - DONE

`Store.maybe_prune()` runs once per calendar day (latched to
the day, called from the record paths): signals and trades
older than `trading.history_retention_days` (default 90, 0 =
keep forever) are dropped and the removal is logged.

## 6. Split account.py - DONE

trader/account.py went from ~1,350 lines to ~490 (transport,
caches, thin delegates) with:

- `trader/ws_common.py` - shared amount/quote parsing and
  account resolution helpers (the leaf every other module uses)
- `trader/paper.py` - PaperAccount, PaperLedger and
  seed_paper_accounts
- `trader/mapping.py` - map_stocks / map_options: raw GraphQL
  nodes into strategy rows (multi-leg grouping, condors,
  butterflies), taking the usd-cad quote as a callable so it
  has no transport dependency

`account.py` re-exports the old names so existing imports keep
working; new code should import from the specific modules.

## 7. Static dashboard assets - DONE

The frontend lives in `trader/web/static/` (dashboard.html /
dashboard.css / dashboard.js) - real files, lintable and
syntax-checkable. `trader/web/dashboard.py` loads and re-exports
them (DASHBOARD_HTML/CSS/JS) so tests and the ui harness keep
one source of truth; the login page stays as the tiny
parameterized template. The page keeps its unconditional
no-store (flask's static handler would default to no-cache),
so a post-update reload can never serve stale assets. Atomic
one-commit deploys are preserved: the updater restarts the
process and everything swaps at once.

## 8. Production WSGI server - DONE

`run.py` serves through `waitress` (16 threads) on plain http
when installed, with Werkzeug as the automatic fallback for
bare checkouts. Caveat found in review: waitress has no native
TLS, so https configs (the self-signed cert path) stay on
Werkzeug - `pick_wsgi(ssl_context, waitress_available)` makes
that explicit and tested. Serving plain http under an https
config would break the health watchdog and the reader's https
posts (that was a live restart loop once).

## 9. Per-thread SQLite connections - DONE

`Store._conn` is a per-thread property (WAL + NORMAL +
busy_timeout on each); writes serialize on `_write_lock`,
reads (positions, trades, meta lookups) run lock-free beside
the writer, and the positions cache has its own tiny lock. A
concurrency test hammers three readers against a writer for
two seconds with zero sqlite errors.

## 10. Package split for trader/ - DONE

The flat 20-module `trader/` folder is now four
sub-packages plus the true core at the root:

- `trader/ws/` - Wealthsimple integration (account, mapping,
  the extracted GraphQL documents, tokens, account types)
- `trader/trading/` - execution logic (executor, parser,
  risk, stops, quotes, strategies, margin, paper, mirror)
- `trader/web/` - the dashboard (server, dashboard, static/)
- `trader/ops/` - process operations (updater, processes,
  supervise, loghook, notify)
- root keeps what everything depends on: config, settings,
  store, pipeline

All import paths updated across run.py, tests and scripts.
The updater's `trader/*` restart pattern still matches nested
paths (fnmatch `*` crosses separators) - verified by test.

## 11. Architecture diagrams - DONE

`docs/architecture.dot` (+ png/svg) shows the full server
layout: the standalone reader feeding alerts over HTTP, the
Flask pipeline (web, alert processing, store, supervised
threads, paper ledger), and the external edges - Wealthsimple
GraphQL, GitHub auto-update, Discord webhooks, and the browser
polling over Tailscale.

`docs/runtime.dot` (+ png/svg) shows the process/thread view:
both .bat restart loops, the pipeline's main thread plus its
five supervised loops (updater, mirror, stop monitor, webhook
batcher, health watchdog) under ops/supervise.py, the reader
process with its locally-supervised webhook thread, and the
crash/hang/update exit paths that feed the restart loops.

## 12. Search over older trades/signals - DONE

Retention is now a year (trading.history_retention_days
default 365) while the dashboard renders only the latest 50
of each. Landed: `Store.search_history` (ticker, action,
status, mode, date range, free text with LIKE-escaping) over
both tables, the auth-guarded `/api/history` endpoint with
pagination, and a dashboard search bar below the trade log -
a single search that always merges alerts and trades (the
kind selector was dropped after review). The merged stream
puts alerts
and the trades they produced side by side, paired via
message_key (the alert rides its trade's timestamp so the
pair stays adjacent, alert on top, both rows highlighted; the
result count notes how many alerts matched trades;
trade-only filters still apply to the trades half). The link
is first-class schema-wise: every alert-driven
record_trade carries the signal's message_key and
idx_trades_message_key backs the lookup (mirrored fills and
stop exits have no originating signal and stay unlinked by
design). The
idx_trades_mode_ts index backs date-ranged scans.

## 13. Thread supervision - DONE

After a docs-only update killed the updater thread: every
background loop runs under `trader/ops/supervise.py` - a crash
or an unexpected return is logged, reported to the webhook,
and the thread relaunches after a backoff. Covered: auto-update,
stop monitor, trade mirror, the pipeline's webhook batcher, the
health watchdog, and the reader's webhook thread (reader-local
wrapper - the reader imports no trader code by design). The
dashboard git line shows `UPDATER STUCK` in red when
last_check goes 3x past the interval, so a dead thread is
visible without reading logs. Thread start calls are idempotent
(restart if not alive).

Main-thread coverage: a crash restarts via the .bat loop; a
HANG is caught by `trader/ops/watchdog.py` - a supervised
thread self-fetches /health every 30s and exits the process
after 5 minutes of consecutive failures (notifying first) so
the restart loop recovers it. Browser-side timers need no
supervision: setInterval invocations are independent, poll
failures surface via the reconnect banner, and page visibility
is the browser's domain.

## Database growth model

Measured and documented in `docs/database_plan.md`: a year
of retention lands at ~5 MB quiet / ~14 MB moderate /
~34 MB heavy - trivial for sqlite. The doc also covers
the maintenance guidance (free-page plateau, WAL bounds,
consistent backups) and the FTS5 recommendation for #12.

## Reviews - all findings resolved

A second review after #1-#5 landed, then a full #1-#12
re-review on the third pass. Everything surfaced is closed:

**Second-review findings (post #1-#5):**

- **create_app megafunction** - resolved via
  `PipelineContext` (trader/web/server.py): the payload
  builders (`_account_summaries`, `_summary_payload`,
  `_paper_positions_payload`, `_positions_payload`,
  `_update_status_payload`, `_real_positions`, `_real_stocks`)
  are module-level functions over the context, directly
  testable without building the app. Routes remain thin
  closures over the context - declarative enough that a full
  blueprint migration would add ceremony without testability.
- **_account_summaries was 46-deep** - split into
  `_account_summary` (one account's row: funding parsing,
  value math, paper merge) and `_margin_metrics` (holdings
  assembly + the shared margin model call). Depth is now
  flat; each concern is a named function.
- **Test hygiene: the psutil stub leaks** - fixed: the reader
  tests stub psutil for import time only, then restore the
  real module for the rest of the session.
- **maybe_prune only runs on writes** - fixed: the prune also
  rides the mirror loop, so a quiet bot still ages rows out
  daily.
- **contracts_for is dead** - deleted with its tests rewired
  onto `account_sizing` / `tier_plan`.
- **moomoo was an undeclared dependency** - now a real
  requirements entry: `moomoo-api` (import name `moomoo`),
  the client for the OpenD quote gateway.
- **WebhookBatcher is unsupervised** - supervised
  (trader/ops/loghook.py), and the reader's WebhookLog thread
  runs under a reader-local supervision wrapper (folded into
  #13).

**#1-#5 re-review (third pass):**

- **#1 margin model** - clean: one Holding shape, one
  compute path, injected rate resolvers. No findings.
- **#2 UI smoke test** - moved to tests/scripts/ui_test.py
  during the scripts cleanup; 17 checks, runs as the final
  phase of tests/scripts/e2e_test.py.
- **#3 batched endpoint** - one gap found and fixed: a
  summary failure used to 502 the whole batch, killing
  trades/signals/settings/update status with it. Now the
  dashboard degrades to the summary error marker while the
  rest still renders (the individual /api/summary keeps its
  502); the client guards the missing accounts list.
- **#4 indexes** - verified: both plans EXPLAIN-verified in
  tests, nothing missing.
- **#5 retention** - the quiet-day prune gap is closed (the
  mirror-loop fix above).

**Third-pass full review (post moomoo/history-search/settings
work):**

- **settings did not persist order_type / place_stop_loss /
  sell_only_if_held** - they applied at runtime but _persist
  never wrote them, so they silently reverted on restart.
  Fixed: the enum/bool fields ride the persist loop, with a
  config-file roundtrip test (the earlier gap slipped because
  no test wrote a real file).
- **merged history search split notify+paper pairs** -
  group_top kept the oldest trade's timestamp (an overwrite
  where a max was needed), so a signal with two trades sharing
  its message_key could float away from the group. Fixed and
  contiguity-tested; the dead sig_keys variable is gone.
- **the daily prune was gated on mirroring** - it rode the
  mirror loop, which only starts when paper.mirror is on: with
  mirroring off a quiet bot never pruned (the earlier fix was
  weaker than this doc claimed). The prune now also rides the
  always-on health watchdog.
- **history_retention_days does not reach the live store**
  (retention is read at construction) - tooltip now says it
  takes effect after restart.
- Noted, not fixed: the ui smoke suite has no history-search
  interaction checks (the orphaned-fragment bug shipped
  because of that gap - the duplicate-id guard now exists, but
  search behavior remains untested at the ui level);
  moomoo_host persists only when set, so clearing it leaves a
  stale yaml value; renderSettings from empty stub data
  defaults the order-type select (harness-only edge).
- Verified clean this pass: margin model paths, sizing/tier
  caps (account override replaces the global - by design),
  paper valuation and seeding, notify+paper dual trade rows,
  updater/supervise/watchdog/loghook/notify error handling,
  reader post unbound-local fix, stale-process ancestor
  protection, settings validation ranges, endpoint auth
  guards, waitress/werkzeug selection.

**#6-#12 re-review (third pass):**

- **#6 account.py split** - one real gap: the six ws_common
  helpers (_amount, _amount_opt, _quote_price,
  effective_accounts, account_label, resolve_account_id) were
  duplicated byte-for-byte in trader/ws/account.py, defeating
  the shared-leaf point of the split. Fixed: account.py
  imports them from ws_common (same import surface).
- **#7 static assets** - stale docstrings: the module and the
  plan doc still said trader/static/ (they live in
  trader/web/static/) and the doc kept the pre-#7 problem
  statement below the DONE header. Both corrected.
- **#8 waitress** - verified: fallback intact, requirements
  noted, request logging actually quieter than the Werkzeug
  path (no access lines at all). No findings (the TLS
  caveat is documented in #8 above).
- **#9 per-thread connections** - verified: threading.local
  releases a dead thread's connection (CPython clears the
  local on thread exit), so supervised restarts don't leak;
  the concurrency test still hammers green. No findings.
- **#10 package split** - verified: no stale flat imports
  anywhere; the updater restart-pattern test still covers the
  nested paths. No findings.
- **#11 diagrams** - architecture.dot gained the
  /api/history row (re-rendered to png/svg). Node paths were
  otherwise current.
- **#12 history search** - one UI nit fixed: switching the
  kind to Signals disabled the status select but kept its
  value, silently sending a stale filter (the store ignored
  it, but the UI misled); it now clears on the switch.

## Done / not pursuing

- startup banners, self-healing updater (fetch checks, stale
  index.lock, ignored-runtime-file healing), stale-process
  cleanup, WAL + busy_timeout, summary caching with
  data-version invalidation, mapped-positions caching per fetch
  generation (all landed). The stale-process cleanup learned
  the Windows venv-launcher trap: a venv python.exe spawns the
  real interpreter with an identical command line, so the
  cleanup now spares its own ancestor chain (launcher + shell)
  instead of killing its parent (exit 15) and restart-looping;
  the .bat loops treat the console ctrl+c exit code as a clean
  stop
- reader stays standalone by design (UIA on Windows, no trader
  imports) - duplication of small helpers is accepted
