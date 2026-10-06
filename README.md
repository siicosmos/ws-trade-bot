# ws-trade-bot

A personal Discord-alert → Wealthsimple auto-trading bot.

A Discord alert service posts option/stock trade ideas. A Windows-only
UI Automation **reader** watches the Discord desktop client and posts
every new message to a Flask **pipeline**. The pipeline parses the
alerts, applies risk gates, and executes them in one of three modes —
**notify** (phone notifications only), **paper** (simulated fills), or
**live** (real Wealthsimple orders) — and serves a self-hosted HTTPS
**dashboard** for positions, trade log, history search, settings, and
manual actions.

Designed to run unattended on one Windows PC: supervised threads, a
health watchdog, `.bat` restart loops, a git-based auto-updater, and
log streaming to a private Discord server. Development and testing
happen on any OS (the pipeline itself is cross-platform; only the
reader needs Windows).

```
docs/architecture.png · docs/runtime.png · docs/db_schema.png
```

## Contents

- [Architecture](#architecture)
- [Alert flow end-to-end](#alert-flow-end-to-end)
- [Trading modes](#trading-modes)
- [Safety systems](#safety-systems)
- [Quotes: moomoo vs Wealthsimple](#quotes-moomoo-vs-wealthsimple)
- [Dashboard](#dashboard)
- [HTTP API reference](#http-api-reference)
- [Database](#database)
- [Configuration reference](#configuration-reference)
- [Deployment (Windows)](#deployment-windows)
- [Development (any OS)](#development-any-os)
- [Testing](#testing)
- [Operations](#operations)
- [Repository layout](#repository-layout)
- [Docs](#docs)
- [Roadmap](#roadmap)

## Architecture

Three processes and one browser:

| component | process | what it does |
|---|---|---|
| **reader** | `reader/discord_reader.py` (own venv, Windows only) | Polls the Discord desktop client via UI Automation (uiautomation), auto-scrolls the message pane, strips UI noise, dedupes, and POSTs new messages to the pipeline. Auto-starts/restarts Discord, navigates servers/channels, follows a channel allowlist. Sends a heartbeat with its live-editable settings every ~10 polls. |
| **pipeline** | `run.py` (Flask + waitress) | The web server and the brain: alert ingest (`POST /alert`), parse → dedupe → risk → execute, the paper ledger, the stop monitor, the trade mirror, settings persistence, the dashboard UI and API, SQLite storage. |
| **reader restart loops** | `scripts/start_pipeline.bat` / `start_reader.bat` | cmd loops that relaunch their process 5s after any nonzero exit. The watchdog exits 1 on a 5-minute hang; the auto-updater exits 77 when `trader/*` code changed. |
| **dashboard** | browser tab | Polls `GET /api/dashboard` every 5s over HTTPS (self-signed cert; remote access via Tailscale). Single-page static assets in `trader/web/static/`. |

External services:

- **Wealthsimple** (GraphQL): account values, positions, option chains,
  quotes, activities, and — in live mode — real orders. The pipeline
  uses allowlisted GraphQL documents extracted from the WS app bundle
  (`trader/ws/ws_positions_query.py`, `ws_security_query.py`) plus the
  `wealthsimple-python` client (tokens in gitignored `ws_tokens.env`
  from `scripts/ws_login.py`). All HTTP calls get a hard 15s timeout
  (`ws/ws_http.py`).
- **moomoo / OpenD** (optional): realtime option/index quotes for the
  stop monitor and the SPX ladder. Read-only; orders never go here.
- **Discord webhooks**: trade notifications, alert embeds, log lines,
  update notices, raw-alert feed.
- **GitHub**: the private repo; the auto-updater pulls and restarts.

### Supervised background threads (in the pipeline process)

All wrapped by `trader/ops/supervise.py` (crash → log + Discord notice +
30s retry):

| thread | module | job |
|---|---|---|
| stop monitor | `trading/stops.py` | quote-driven protection for open positions (see [Safety systems](#safety-systems)) |
| trade mirror | `trading/mirror.py` | books real fills from the WS activity feed, reconciles estimated live bookings, sweeps stale pending orders |
| auto-updater | `ops/updater.py` | git fetch + ff-only pull every interval; restarts only when server code changed |
| health watchdog | `ops/watchdog.py` | self-fetches `/health`; exits 1 after 5 min of failure → restart loop recovers |
| webhook batcher | `ops/loghook.py` | tees stdout/stderr log lines to Discord in 3s batches |

## Alert flow end-to-end

1. **Discord client** — the alert author posts in an allowed channel.
   Discord must run with `--force-renderer-accessibility`
   (`scripts/start_discord.bat`) so its UI tree is exposed.
2. **reader** — UIA poll loop (default 0.5s) reads on-screen messages
   (auto-scrolled to bottom — Discord only exposes scrolled-in rows),
   strips chrome/timestamps/reactions, dedupes via a persisted seen-set
   (`.reader_seen.json`) + reaction-prefix detection, filters by the
   `reader.channels` allowlist.
3. **Delivery** — each new message is POSTed to `reader.pipeline_url`
   (default `http://localhost:8080/alert`) as JSON
   `{text, author, ts, parsed_ts, channel}` with header
   `X-Auth-Token: <reader.auth_token>` (must match
   `pipeline.auth_token`). Messages count as seen only after a 2xx;
   failed posts retry from a pending queue, so a pipeline restart never
   swallows alerts.
4. **Pipeline ingest** — `auth_guard` validates the token, then
   `pipeline.process_alert()`:
   - **parse** (`trading/parser.py`): option alerts
     (`BOUGHT [MORE] 0DTE SPX 759C @ 2.5`), stock alerts
     (`BOUGHT XYZ shares @ 12`), size keywords (lotto/micro/tiny/
     small/medium/large/big/full, "hero or zero", "profits only"),
     corrections (`is_correction`), custom regexes from
     `parser.custom_patterns`.
   - **dedupe + record**: sha256 message key into `signals`; sells are
     checked against the last buy for strike/right mismatches.
   - **risk engine** (`trading/risk.py`, see
     [Safety systems](#safety-systems)) — blocks or allows.
   - **branch by mode** (see [Trading modes](#trading-modes)).
5. **Mirror thread (live)** — reconciles estimates against actual
   fills; books the "real" ledger; sweeps stale pending orders.
6. **Stop monitor** — every `trading.stop_check_seconds`, prices each
   open option position and fires exits through the executor.
7. **Store** — everything lands in `trades.db` (SQLite, WAL).
8. **Dashboard** — polls `GET /api/dashboard` every ~5s; **Discord**
   — result embeds to the configured webhook (your phone).

## Trading modes

`trading.mode` selects the execution path. `paper.enabled: true` adds
paper execution *alongside* notify mode (notify + paper = paper with
phone notifications). Paper accounts are seeded once from the live
value and positions (`seed_paper_accounts`); `paper.mirror: true`
copies your own real fills into the paper ledger at actual prices.

| mode | execution | ledger | notifications |
|---|---|---|---|
| **notify** | none — per-account **sizing preview** via `account_sizing` | dry-run "notified" rows | rich alert embed + sizing lines to your phone |
| **paper** | `PaperExecutor` — simulated fills into the positions ledger, paper equity adjusted (×100 options, FX) | `mode="paper"` rows per account | result embeds (quiet by design) |
| **live** | `WealthsimpleExecutor` — resolves the option chain, places limit (ask + `limit_offset_pct`) or market orders, books an **estimated** position immediately + a `pending_orders` row | `mode="real"` rows (actual fills, from the mirror) + estimates pending reconciliation | result embeds; the mode badge pulses red |

Sizing core (`trading/executor.py`):

- `tier_plan`: budget = account value × tier `risk_pct_max`; contracts
  = budget ÷ premium × 100, clamped by tier min/max and
  `max_contracts_per_trade`.
- Per-account overrides: `risk_per_trade_pct`,
  `max_contracts_per_trade`, `max_open_risk_pct` on each
  `wealthsimple.accounts[]` entry.
- Stock buys use `stock_size_tiers` (% of account per size keyword).

## Safety systems

### Pre-trade gates — `RiskEngine.evaluate`

| gate | knob | default | behavior |
|---|---|---|---|
| kill switch | `trading_paused` | false | blocks every new BUY; exits stay allowed. Toggled live from settings |
| ticker whitelist | `ticker_whitelist` | [] | only these tickers; empty = all |
| daily trade cap | `max_trades_per_day` | 5 | per account, resets midnight |
| cooldown | `cooldown_seconds` | 60 | between trades on the same contract |
| loss-streak breaker | `max_consecutive_losses` | 2 | stops option buys for the day after N losses in a row |
| daily-loss breaker | `max_daily_loss_pct` | 0 (= off) | blocks all new buys once today's realized pnl sinks below this % of account value |
| min DTE | `min_dte_days` | 0 | skips options expiring sooner |
| dedupe window | `dedupe_window_minutes` | 10 | same alert text inside the window is ignored |

### Position caps — `trading/executor.py`

| cap | knob | default | behavior |
|---|---|---|---|
| open-risk cap | `max_open_risk_pct` (global, per-account override) | 30 | max % of account value in open positions; new buys skip at the cap |
| cluster cap | `cluster_cap_pct` | 50 | correlation-aware: one (underlying, expiry, right) cluster may never exceed this % of value — a buy into a capped cluster skips even when the global cap has room (`store.open_risk_clusters`) |
| tier caps | `size_tiers` | lotto 0.5% → full 10% | risk budget + contract count per size keyword |
| lotto gain cap | `lotto_gain_budget_pct` | — | profits-only alerts cap gains reinvestment |
| sell-only-if-held | `sell_only_if_held` | true | never sells contracts the account doesn't hold |

### Stop monitor — `trading/stops.py`

Runs whenever any mode executes (paper, live, or paper-alongside-
notify) — even with `quotes.enabled: false`:

- **paper / notify+paper**: prices from the paper ledger's own quote
  map (live WS position nodes → WS chains → moomoo).
- **live**: falls back to WS option chains directly — an executing
  mode never runs unguarded; when even that fails, the pipeline logs
  a loud "no automated protection" warning.

Checks every `stop_check_seconds`; per open option position gets a
bid, tracks the peak, and the **effective stop** is:

- per-tier `stop_loss_pct` (from `size_tiers`) over the global
  `stop_loss_pct` — a % drop from entry → `[STOP]` sell;
- **trailing**: global `trailing_stop_pct` or the position's own
  `trail_pct` (set per position from the dashboard; overrides the
  global, can enable it when global is off, or disable it with 0) —
  % off the position's peak bid;
- **back-to-entry** (`back_to_entry_enabled`): 0DTE protection —
  exits at entry when the bid decays back to it → `[B2E]`;
- **per-position take-profit** (`tp_gain_pct`): sells the whole
  remaining position when the bid reaches entry × (1 + tp%) → `[TP]`,
  runs even with global stops off.

Fires real sells through the executor (live) or paper sells (paper),
records trades, notifies Discord.

### Live fill reconciliation — `trading/mirror.py`

The live executor books an *estimated* position immediately; the
mirror thread makes the ledger honest:

- every live order records a `pending_orders` row (contract, action,
  qty, estimated price, pre-order position snapshot);
- actual fills (WS activity feed, deduped by canonicalId) reconcile
  against the oldest matching open order — qty/avg corrected exactly,
  realized pnl from the real basis (`realized_today` moves by the
  same correction so the daily-loss breaker stays honest);
- a fill landing beyond `max_slippage_pct` (default 2%) from the
  estimate posts a Discord notice with both prices (data only);
- orders older than 300s with no fill are reversed exactly
  (pre_qty/pre_avg restored);
- **partial fills** accumulate on the pending order (filled qty +
  blended price); a partial past the TTL keeps its FILLED part booked;
  a partially-filled order whose market price ran
  `partial_fill_cancel_pct` (default 10%) away from the estimate gets
  its remainder cancelled at the broker immediately;
- the mirror pass runs unconditionally in live mode.

## Quotes: moomoo vs Wealthsimple

`quotes.enabled` + `quotes.provider` decide where live pricing comes
from. With `provider: moomoo` (and OpenD running at
`quotes.moomoo_host:port`):

| uses moomoo | stays on WS |
|---|---|
| stop-monitor bids (stops, trailing, TP, B2E) | account values, net-liquidation, USD conversion, funding balances |
| SPX index spot + SPY spot for the levels ladder (`/api/spx`) | real positions on the dashboard |
| paper-only contract pricing (no live counterpart) — one batch snapshot | paper pricing of contracts the real account holds (position `quoteV2`) |
| | order placement, chain resolution for orders |
| | fill reconciliation (activity feed) |
| | margin rates, account types, USD:CAD FX |
| | SPX fallback (index quote or SPY × 10.0391, market status) |

If OpenD is down, `make_quote_provider` falls back to WS option chains
for the stop monitor and the ladder falls back to WS — you'd only
notice via the startup line. Everything that touches money (orders) is
WS-only regardless; moomoo is read-only pricing.

## Dashboard

Single-page static assets (`trader/web/static/dashboard.{html,css,js}`),
served by Flask with the CSS inlined. Auth: username+password sessions
(`users` table, pbkdf2, first boot claims the admin account from the
access token) or the legacy `X-Auth-Token` machine token for scripts
and the reader. 5-fail/15-minute login lockout.

- **header badges**: mode (pulses red in live), paper, reader state,
  git commit, stops state; **currency toggle**; **eye mask**.
- **account cards**: real card (value, margin requirement + breakdown,
  open risk, realized today, cluster flag, per-account cap) and paper
  cards seeded from live.
- **open positions table** (options) with per-row tp/trailing guard
  button and a manual sell button (live: real order through the
  executor; greyed out outside live mode); **stock holdings** toggle.
- **recent alerts** (with ignored-chatter filter) and **trade log**.
- **history search**: merged alert/trade stream, ticker/action/status/
  mode/date filters, free text.
- **SPX levels ladder**: realtime SPX/SPY spot + saved levels text
  (cross-device source of truth).
- **settings modal**: everything from [Configuration
  reference](#configuration-reference) marked "editable" — applied and
  persisted to `config.yaml` atomically without a restart; help text
  renders under each field on touch screens.
- **users panel** (admin): create/delete users, change passwords.

## HTTP API reference

All API paths require auth (browser session or `X-Auth-Token` header)
except `/health`, `/favicon.ico`, and `/login`. Admin-gated routes
check the session role.

| method | path | auth | purpose |
|---|---|---|---|
| GET/POST | `/login` | none | login page / credential check; first boot claims admin; 5-fail/15-min lockout |
| GET | `/logout` | session | clear session, redirect |
| GET | `/` | session | dashboard page |
| GET | `/health` | none | liveness `{"status":"ok"}` (watchdog target) |
| GET | `/api/summary` | any | account cards summary (mode, values, margin, risk, reader, stops) |
| GET | `/api/dashboard` | any | one batched poll: summary + paper positions + positions + signals + trades + settings + update status + me (cached, stale-while-revalidate) |
| GET | `/api/positions` | any | open positions (live WS rows where fetchable, ledger rows otherwise) |
| GET | `/api/paper-positions` | any | paper holdings with live pricing and pnl per account |
| POST | `/api/paper-reset` | admin | reset a paper account's ledger and re-seed from live |
| POST | `/api/paper-resize` | admin | bring past paper STOCK positions up to tier sizing |
| POST | `/api/paper-sell` | admin | manual close of a paper position at its live price (whole or given qty) |
| POST | `/api/position-sell` | admin | live mode: manual REAL sell of a tracked live position at the current bid (stop-monitor path) |
| POST | `/api/position-tp` | admin | set/clear per-position take-profit (`tp_gain_pct`) and trailing (`trail_pct`) guards |
| GET | `/api/signals?limit=` | any | recent recorded signals |
| GET | `/api/trades?limit=` | any | recent trade log |
| GET | `/api/history` | any | search retained history (kind, ticker/action/status/mode, since/until, q, limit/offset) |
| GET | `/api/spx` | any | realtime SPX/SPY spot for the ladder (moomoo or WS, SPY×10.0391 proxy fallback) |
| POST | `/api/spx-levels` | any | save the pasted SPX levels text |
| GET | `/api/settings` | any | the dashboard-editable settings view |
| POST | `/api/settings` | admin | validate + apply + persist settings (no restart) |
| GET | `/api/update_status` | any | auto-updater status (last check/result/commit/branch) |
| POST | `/api/reader_status` | token | reader heartbeat (`{channel, ok}`); returns live reader config — the reader merges it |
| GET | `/api/reader_status` | any | current reader heartbeat state (dashboard reader line) |
| GET | `/api/users` | admin | list users (no hashes) |
| POST | `/api/users` | admin | create/delete/set_password (self-change needs current password; cannot delete self or last admin) |
| POST | `/alert` | token | **alert ingest** — JSON `{text, author, ts, parsed_ts, channel}`; runs `process_alert` |

## Database

SQLite (`trades.db`), WAL, per-thread connections, writes serialized,
reads lock-free. Schema: `docs/db_schema.png`.

| table | purpose |
|---|---|
| `signals` | every received message (parsed or chatter) — message_key (sha256) PK, text, author, channel, correction flag |
| `trades` | one row per executed/notified/blocked action — mode, action, ticker/contract, qty, price, status, detail, links to the signal via message_key/dedupe_key |
| `positions` | per (mode, account, contract_key): qty, avg_premium, realized, peak_bid, size, tp_gain_pct, trail_pct — the paper/real ledgers ride this |
| `pending_orders` | live-order estimates awaiting reconciliation: est price, pre-order snapshot, filled qty/price, status |
| `meta` | kv: paper seeds/equity, mirror cursors, realized_today, loss streak, value caches, SPX levels text |
| `users` | dashboard accounts: pbkdf2 (200k iters) hash, role, login timestamps |

Size (~280 B/signal row, ~850 B per actionable alert with its trades):
one year of heavy use (150 chatter + 60 alerts/day) ≈ 34 MB. Daily
prune of rows past `history_retention_days` (365) rides the write
paths and the watchdog; the file plateaus by design (freed pages are
reused, no automated VACUUM).

Backups: `sqlite3 trades.db ".backup backup.db"` while running (a raw
copy can miss WAL contents). Clean slate: `scripts/clean_start.py`.

## Configuration reference

Full commented template: `config.example.yaml`. Dashboard-editable
settings are applied live via `/api/settings` and persisted to
`config.yaml`; the rest needs a restart.

### `pipeline` — web server
`host` (0.0.0.0), `port` (8080), `auth_token` (**required** when
binding non-localhost — the server refuses to start otherwise; the
dashboard password and the reader's credential),
`tls_cert`/`tls_key` (empty = plain HTTP).

### `trading` — mode + sizing + risk
`mode` (notify|paper|live), `order_type` (limit|market),
`limit_offset_pct` (0.5), `position_size_cad` (100),
`risk_per_trade_pct` (5), `stock_size_tiers` (% per size keyword),
`size_tiers` (lotto→full: risk_pct_max, contracts_min/max — options),
plus every knob from [Safety systems](#safety-systems):
`max_contracts_per_trade` (10), `max_open_risk_pct` (30),
`cluster_cap_pct` (50), `max_slippage_pct` (2),
`partial_fill_cancel_pct` (10), `stop_loss_pct` (25),
`trailing_stop_pct` (0), `stop_check_seconds` (30),
`back_to_entry_enabled`, `lotto_gain_budget_pct`,
`max_consecutive_losses` (2), `max_daily_loss_pct` (0),
`min_dte_days` (0), `max_trades_per_day` (5),
`cooldown_seconds` (60), `dedupe_window_minutes` (10),
`ticker_whitelist` ([]), `skip_underlyings` ([]),
`sell_only_if_held` (true), `trading_paused` (false),
`paper_account_value` (10000), `history_retention_days` (365).

### `wealthsimple` — accounts
`accounts[]`: `account_id`, `label`, `enabled`, and per-account
overrides `risk_per_trade_pct` / `max_contracts_per_trade` /
`max_open_risk_pct` / `paper_value`. Plus `exchange_hint`,
`stock_margin_rate` (0.30), `margin_rate_overrides` ({}),
`positions_refresh_seconds` (30), `values_refresh_seconds` (60).

### `quotes` — stop-monitor pricing
`enabled` (false — the settings toggle does it live),
`provider` (ws|moomoo), `moomoo_host` (127.0.0.1),
`moomoo_port` (11111). See [Quotes](#quotes-moomoo-vs-wealthsimple).

### `paper`
`enabled` (false — paper alongside notify), `mirror` (true — copy
real fills into the paper ledger at actual prices),
`mirror_interval_seconds` (60).

### `reader` — the Windows Discord watcher
`pipeline_url`, `poll_interval` (0.5), `auth_token` (**must match
`pipeline.auth_token`**), `channel_marker`, `max_items` (40),
`auto_scroll`, `auto_start_discord`, `auto_switch_channel`,
`discord_server`, `channel_servers` map, `discord_reopen_seconds`
(15), `discord_restart_seconds` (90), `discord_start_command`,
`channels` allowlist (**never** include your webhook output channel).
Most knobs are editable live from the dashboard — the reader merges
them from its heartbeat.

### `discord` — webhooks
`notify` (master switch), `webhook_url` (trade alerts to your phone),
`reader_log_webhook_url`, `pipeline_log_webhook_url`,
`update_webhook_url`, `raw_alert_webhook_url` (raw alert feed).

### `auto_update`
`enabled` (true), `interval_seconds` (600). Restarts (exit 77) only
when `trader/*`, `run.py`, or `requirements.txt` changed.

### `parser`
`custom_patterns` ([]) — extra regexes for alert formats the built-in
parser misses.

## Deployment (Windows)

1. **Clone** the repo (or run `scripts/setup_ssh.ps1` to set up the
   SSH key for unattended auto-update pulls).
2. **Pipeline**: `scripts/start_pipeline.bat` — creates the venv,
   installs requirements, copies `config.example.yaml` → `config.yaml`
   if missing, runs `run.py`, restarts on any nonzero exit.
3. **Wealthsimple login**: `python scripts/ws_login.py` — interactive
   (email/password/2FA); saves access+refresh tokens to gitignored
   `ws_tokens.env` and prints account IDs for config.
4. **Config**: set `pipeline.auth_token` (generate with
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`),
   the `wealthsimple.accounts[]` IDs/labels, and the trading knobs.
5. **Reader**: `scripts/start_reader.bat` (own venv from
   `reader/requirements-windows.txt`), and `scripts/start_discord.bat`
   to launch Discord with `--force-renderer-accessibility`. Set
   `reader.auth_token` to match the pipeline and the channel allowlist.
6. **HTTPS + remote access**: `python scripts/gen_cert.py` for a
   self-signed cert (`pipeline.tls_cert/tls_key`); Tailscale for
   remote dashboard access from your phone.
7. **moomoo (optional)**: install OpenD, log in, set
   `quotes.provider: moomoo` (or via settings).
8. First boot: open the dashboard, log in with the access token to
   claim the admin account, set a password.

## Development (any OS)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml   # keep host 127.0.0.1 for a dev stub
.venv/bin/python run.py -c config.yaml
```

The pipeline runs fine on Linux (the reader does not — it needs the
Windows UIA). A dev `config.yaml` with `mode: notify` + `paper.enabled:
true` on 127.0.0.1 exercises the whole pipeline without real orders.

## Testing

```bash
# unit/integration suite (~400 tests, stubs for the reader's UIA)
.venv/bin/python -m pytest tests -q --ignore=tests/scripts

# full-stack black-box E2E (boots the real server on a temp config/db)
.venv/bin/python tests/scripts/e2e_test.py

# headless-Chrome dashboard smoke test (needs chrome on PATH)
.venv/bin/python tests/scripts/ui_test.py

# JS syntax check after editing the dashboard
node --check trader/web/static/dashboard.js
```

Key suites: `test_pipeline.py` (end-to-end alert processing),
`test_parser.py`, `test_reader.py`, `test_dashboard.py` (HTML/CSS/JS
invariants), `test_stops.py`, `test_mirror.py`,
`test_live_executor.py` (every live path against a fake ws client,
incl. the kill switch), `test_settings_update.py`, `test_users.py`,
`test_updater.py`, `test_store_migration.py`.

## Operations

- **restart chain**: watchdog exits 1 after a 5-min hang →
  `start_pipeline.bat` relaunches after 5s; the auto-updater exits 77
  when server code changed; `run.py` kills stale copies of itself
  holding the port (`ops/processes.py`) and prints the previous exit
  code from `pipeline_exit.txt`.
- **logs**: pipeline.log + reader.log, both optionally teed to Discord
  in 3s batches (`install_log_webhook`).
- **updates**: `git fetch` + ff-only pull every
  `auto_update.interval_seconds`; dirty runtime files are healed;
  `.last_update.json` records the last pull; the dashboard shows the
  running commit.
- **known-exit codes**: 77 = code update restart, 1 = watchdog hang
  detection, anything else = crash (restart loop catches it).
- **expectancy report**: `python scripts/expectancy.py` — per-tier win
  rate / avg win / avg loss / expectancy from closed paper round
  trips; drives the "set `contracts_max: 0` on negative-expectancy
  tiers" decision rule.
- **diagnostics**: `python scripts/diagnose.py` (deps, config,
  connectivity); `python scripts/dump_discord_tree.py` (reader
  debugging).
- **go-live**: see `docs/live_readiness_plan.md` for the checklist,
  suggested starting settings, and the safety-system reference.

## Repository layout

```
run.py                     # pipeline entry point: wiring + threads + server
config.example.yaml        # documented config template
config.yaml                # local config (gitignored)
trades.db                  # SQLite state (gitignored)
reader/
  discord_reader.py        # UIA Discord watcher (standalone, Windows)
  inspect_discord.py       # Discord window utilities + CLI diagnostic
  requirements-windows.txt
trader/
  config.py                # YAML → dataclass config (load_config)
  settings.py              # dashboard-editable settings (get/apply/persist)
  store.py                 # SQLite persistence + migrations
  pipeline.py              # process_alert: parse → dedupe → risk → execute
  trading/
    parser.py              # alert regexes, Alert dataclass, contract keys
    executor.py            # sizing core, PaperExecutor, WealthsimpleExecutor
    risk.py                # RiskEngine pre-trade gates
    paper.py               # PaperAccount, PaperLedger, seeding
    stops.py               # StopMonitor: stops/trailing/TP/B2E
    quotes.py              # quote providers: moomoo OpenD / WS chains
    mirror.py              # real-fill ledger + pending-order reconciliation
    margin.py              # shared margin model (real + paper cards)
    strategies.py          # multi-leg option structure naming
  ws/
    account.py             # WealthsimpleAccount (cached transport)
    mapping.py             # GraphQL rows → dashboard rows
    ws_positions_query.py  # the app's FetchIdentityPositions document
    ws_security_query.py   # the app's FetchSecurity document
    ws_http.py ws_common.py ws_tokens.py account_types.py
  web/
    server.py              # Flask app: auth, routes, caches
    dashboard.py           # page loaders, login template
    static/dashboard.{html,css,js}
  ops/
    notify.py loghook.py processes.py supervise.py updater.py watchdog.py
scripts/                   # .bat launchers, ws_login, gen_cert, expectancy,
                           # diagnose, clean_start, setup_ssh, dump_discord_tree
tests/                     # pytest suite (+ scripts/: e2e_test, ui_test)
docs/                      # diagrams (.dot/.png/.svg) + live_readiness_plan.md
certs/                     # self-signed TLS (gitignored)
```

## Docs

| file | content |
|---|---|
| `docs/architecture.png` | component architecture (external services, pipeline cluster, data flows) |
| `docs/runtime.png` | processes and threads at runtime, restart chain |
| `docs/db_schema.png` | trades.db schema |
| `docs/live_readiness_plan.md` | live-trading safety-system reference, go-live checklist, suggested starting settings |

Re-render the diagrams after editing the `.dot` sources:

```bash
dot -Tpng -o docs/architecture.png docs/architecture.dot
dot -Tsvg -o docs/architecture.svg docs/architecture.dot
# (same for runtime.dot and db_schema.dot)
```

## Roadmap

Open ideas, in no particular order:

1. **Enforce username+password login** — remove the bare-token browser
   fallback: the access token stays as the machine credential for the
   reader/scripts and seeds the admin on upgrade, but can no longer
   open a browser session on its own. Change-password form gets a
   new+confirm pair with a mismatch check.
2. **Roles: admin and regular user** — replace the viewer role; regular
   users see the dashboard, only admins manage users and settings;
   per-user dashboard preferences stored per user instead of globally.
3. **User-related settings move to the database** — per-user
   preferences (UI state, notifications) move from `config.yaml` into
   the `users` table; config.yaml keeps only machine and pipeline
   settings.
4. **Keep diagrams current** — re-render the dot sources when the
   schema, architecture, or runtime layout change.
