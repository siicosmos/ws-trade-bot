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

## 4. SQLite indexes - MEDIUM

- `trades(ts)` and `trades(mode, ts)` for `trades_today` /
  `loss_streak` windows
- `positions(mode, account)` for `open_risk`

Tables are small today; each is a one-line migration in
`Store.__init__` and keeps queries O(small) forever.

## 5. Retention for signals/trades - MEDIUM

Neither table expires. Add a daily-rollover cleanup (delete
rows older than 90 days) so queries and the dashboard stay
fast without periodic clean starts.

## 6. Split account.py - LOW (bigger refactor)

trader/account.py (~1,350 lines) mixes four concerns:

- WS transport (client, extracted GraphQL documents)
- the margin model (now extracted - see #1)
- the paper ledger / seeding
- position mapping and caching

Split into `ws_client` / `paper` / `mapping` modules, each
testable in isolation. The margin extraction was the first
slice; the rest can follow the same pattern.

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

## Done / not pursuing

- startup banners, self-healing updater (fetch checks, stale
  index.lock, ignored-runtime-file healing), stale-process
  cleanup, WAL + busy_timeout, summary caching with
  data-version invalidation, mapped-positions caching per fetch
  generation (all landed)
- reader stays standalone by design (UIA on Windows, no trader
  imports) - duplication of small helpers is accepted
