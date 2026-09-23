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

`scripts/ui_test.py` renders the real dashboard in headless
chrome (`--dump-dom`) with a stubbed `api()`, 17 checks covering
the interactions that historically broke while unit tests
passed: paper currency flip, eye masking of cash/seeded, margin
breakdown open/mask/arrow states, holdings arrow on an empty
ledger, negative-zero rendering. Runs as the final phase of
`scripts/e2e_test.py`, soft-skips without a browser on PATH
(`CHROME_BIN` overrides).

## 3. Batched dashboard endpoint - DONE

`/api/dashboard` composes summary, paper positions, positions,
signals, trades, settings and update status in one response
(payload builders shared with the individual routes, which
remain available). The client's 5s poll went from seven
requests to one; every button action refreshes through the
same round trip; loaders are pure render functions.

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

## 7. Static dashboard assets - LOW (deployment trade-off)

trader/dashboard.py (~1,220 lines) is a Python string
containing the whole frontend: no JS linting, no syntax
highlighting, edits via string replacement. Moving HTML/JS/CSS
to `trader/static/` served by Flask is the biggest
maintainability win.

Trade-off: the single-string approach gives atomic one-commit
deploys with zero static-path concerns - the updater restarts
the process and everything swaps at once. Static files also
need cache-busting (the page currently sends no-store, so it
would be fine, but it is a behavior to preserve deliberately).

## 8. Production WSGI server - LOW

`app.run(threaded=True)` is Werkzeug's dev server. Works for one
user over Tailscale, but `waitress` is a drop-in replacement if
polls ever drop under load:

    pip install waitress
    waitress-serve --port=8080 run:app

## 9. Per-thread SQLite connections - LOW (only if needed)

All requests funnel through one connection guarded by a global
lock. WAL is already enabled. If the dashboard ever feels
sluggish under concurrent polls + mirror writes, switch `Store`
to a connection factory per thread instead.

## Thread supervision (landed, unplanned)

After a docs-only update killed the updater thread: every
background loop (auto-update, stop monitor, mirror) now runs
under `trader/supervise.py` - a crash or an unexpected return
is logged, reported to the webhook, and the thread relaunches
after a backoff. The dashboard git line shows `UPDATER STUCK`
in red when last_check goes 3x past the interval, so a dead
thread is visible without reading logs. Thread start calls are
idempotent (restart if not alive).

## Second review findings (post #1-#5)

Fresh pass after the margin extraction, UI smoke test, batched
endpoint, indexes and retention landed:

- **create_app megafunction** (trader/server.py, ~740 lines,
  83-branch depth). Every route and helper is a closure over
  cfg/store/account, so nothing is testable without building
  the whole app (the test fixtures are correspondingly heavy).
  Refactor: a PipelineContext dataclass + Flask blueprint;
  `_account_summaries` and `_paper_card_metrics` become module
  functions taking the context. This is the server-side half
  of #6.
- **_account_summaries is still 46-deep** even after the
  margin extraction - registered-plan detection, funding
  parsing, margin assembly and the paper merge all inline.
  Extract a per-account summary builder.
- **Test hygiene: the psutil stub leaks** - tests/test_reader.py
  globally does `sys.modules.setdefault("psutil", MagicMock())`
  for the whole session; it already bit the stale-process test.
  Scope it to a fixture so later tests get the real module.
- **maybe_prune only runs on writes** - a day with no alerts
  means no prune, so old rows can outlive the retention window
  indefinitely on a quiet bot. Hook it into the mirror or
  updater loop too, or accept the laziness.
- **contracts_for is dead** - executor.py:78 legacy sizing
  helper referenced only by its own tests; account_sizing
  superseded it. Delete with its tests.
- **WebhookBatcher is unsupervised** - the only background
  loop left outside trader/supervise.py (its body is trivially
  safe, but the policy is now 'every thread supervised').
- **moomoo is an undeclared optional dependency** -
  quotes.provider=moomoo lazily imports the moomoo package;
  requirements.txt says nothing about it.

## Done / not pursuing

- startup banners, self-healing updater (fetch checks, stale
  index.lock, ignored-runtime-file healing), stale-process
  cleanup, WAL + busy_timeout, summary caching with
  data-version invalidation, mapped-positions caching per fetch
  generation (all landed)
- reader stays standalone by design (UIA on Windows, no trader
  imports) - duplication of small helpers is accepted
