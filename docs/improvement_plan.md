# Improvement Plan

Architecture and performance review of the codebase (~9.9k lines),
in priority order. Each item is independent - pick them off as
time allows.

## 1. Shared margin model (correctness) - DONE

Landed: `trader/margin.py` (Holding / compute_requirement /
resolve_rate) is the single source of truth; both the real-card
computation in `_account_summaries` and `_paper_card_metrics`
build normalized Holdings and call in. Unified breakdown line
formats across both cards.

The requirement computation is inline in `_account_summaries`
(trader/server.py) while `_paper_card_metrics` reimplements a
parallel version. They have drifted subtly:

- the real card resolves per-security rates via
  `security_margin_rate` (security_id lookup)
- the paper card only uses symbol overrides / the default rate

Extract one module (e.g. `trader/margin.py`) with a single
`compute_margin_metrics(holdings, fx, rates, ...)` used by both
paths. This is the money math - it should exist exactly once.

## 2. Headless-Chrome UI smoke test - HIGH - DONE

Landed: `scripts/ui_test.py` - canned-data page driven by
headless chrome (`--dump-dom`), 17 checks covering the
interactions that historically broke (paper flip, eye masking,
breakdown open/mask/arrow, empty-ledger arrow, negative-zero).
Runs as part of `scripts/e2e_test.py`, soft-skips without a
browser on PATH.

Every string-grep test can pass while the browser behaves
differently. Session experience: three real bugs (static arrow on
empty ledgers, unmasked cash, dead currency flip) passed all 267
unit tests and only reproduced in a real browser.

Add `scripts/ui_test.py`:

- boot the Flask app on a test port with fixture accounts
- generate a test page from DASHBOARD_HTML with a stubbed `api()`
  (canned summary JSON)
- drive it with `google-chrome --headless=new --dump-dom`
- assert on rendered DOM (card HTML, button text after clicks)

The harness pattern already exists in the dev notes; it just
needs to live in the repo and run in CI/e2e.

## 3. Batched dashboard endpoint - MEDIUM - DONE

Landed: `/api/dashboard` composes summary, paper positions,
positions, signals, trades, settings and update status in one
response (payload builders shared with the individual routes,
which still exist). The client `load()` does a single fetch and
one render pass; every button action refreshes through the same
round trip. The 5s poll went from seven requests to one.

The 5s poll fires six requests (summary, positions, signals,
trades, settings, update_status). One `/api/dashboard` endpoint
returning all of them:

- one fetch instead of six (fewer log lines, less Tailscale
  proxy noise during restarts)
- one cache/version check instead of six
- simplifies the client `load()` to a single render pass

## 4. SQLite indexes - MEDIUM

- `trades(ts)` and `trades(mode, ts)` for `trades_today` /
  `loss_streak` windows
- `positions(mode, account)` for `open_risk`

Tables are small today; each is a one-line migration in
`Store.__init__` and keeps queries O(small) forever.

## 5. Retention for signals/trades - MEDIUM

Neither table expires. Add a daily-rollover cleanup (delete rows
older than 90 days) so queries and the dashboard stay fast
without periodic clean starts.

## 6. Split account.py - LOW (bigger refactor)

trader/account.py (~1,350 lines) mixes four concerns:

- WS transport (client, extracted GraphQL documents)
- the margin model
- the paper ledger / seeding
- position mapping and caching

Split into `ws_client` / `margin` / `paper` / `mapping` modules,
each testable in isolation. Do this after #1 (the margin model
extraction is the first slice of it).

## 7. Static dashboard assets - LOW (deployment trade-off)

trader/dashboard.py (~1,220 lines) is a Python string containing
the whole frontend: no JS linting, no syntax highlighting, edits
via string replacement. Moving HTML/JS/CSS to `trader/static/`
served by Flask is the biggest maintainability win.

Trade-off: the single-string approach gives atomic one-commit
deploys with zero static-path concerns - the updater restarts
the process and everything swaps at once. Static files also need
cache-busting (the page currently sends no-store, so it would be
fine, but it is a behavior to preserve deliberately).

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

## Done / not pursuing

- startup banners, self-healing updater, stale-process cleanup,
  WAL + busy_timeout, summary caching with data-version
  invalidation, mapped-positions caching per fetch generation
  (all landed)
- reader stays standalone by design (UIA on Windows, no trader
  imports) - duplication of small helpers is accepted
