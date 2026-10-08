# ws-trade-bot

A personal Discord-alert → Wealthsimple auto-trading bot, built as
**one alert source + many independent trading apps**.

A Discord alert service posts option/stock trade ideas. A Windows-only
UI Automation **reader** watches the Discord desktop client and posts
every message to the **info server** — a Flask app that parses the
alerts, records them, and serves them as an **alert feed** (push +
long-pull). Each trader runs their own **consumer app**: the same
codebase in a consumer role, which pulls the feed and runs the full
trading pipeline locally — risk gates, sizing, paper/live execution
against **their own Wealthsimple login**, their own stop monitor, fill
reconciliation, SQLite store, Discord webhook, and dashboard. The
server holds no per-user state and never touches money.

Designed to run unattended: supervised threads, a health watchdog,
`.bat` restart loops, a role-split auto-updater (the consumer follows
the rolling GitHub Release; the info server pulls git and the reader
rides that pull), and log streaming to a
private Discord server. Development and testing happen on any OS (the
pipeline itself is cross-platform; only the reader needs Windows).

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
- [Deployment](#deployment)
- [Development (any OS)](#development-any-os)
- [Testing](#testing)
- [Operations](#operations)
- [Repository layout](#repository-layout)
- [Docs](#docs)
- [Roadmap](#roadmap)

## Architecture

Three kinds of processes. One machine can run several of them (the
owner's box typically runs all three).

| component | process | what it does |
|---|---|---|
| **reader** | `reader/discord_reader.py` (own venv, Windows only) | Polls the Discord desktop client via UI Automation (uiautomation), auto-scrolls the message pane, strips UI noise, dedupes, and POSTs new messages to the info server. Auto-starts/restarts Discord, navigates servers/channels, follows a channel allowlist. Sends a channel/ok heartbeat every ~10 polls. |
| **info server** | `run.py -c info/info.config.yaml` (`info:` section) | The alert source: reader ingest (`POST /alert`), parse + dedupe + record, the **alert feed** for consumers (long-poll `GET /api/feed` + push fan-out to registered consumers), the SPX levels text, and a lean admin dashboard (consumer health, feed activity, reader line, levels editor). **No trading wiring** — no executors, no stop monitor, no mirror, no quotes. |
| **consumer app** | `run.py -c consumer/consumer.config.yaml` (`consumer:` section) | The trading app — everything downstream of parsing, local to each user: risk engine, sizing, paper/live executors against **their own Wealthsimple login**, stop monitor, fill reconciliation, own SQLite store, own Discord webhook, and the full dashboard on localhost. Alert sources: the server's push (its `/alert` endpoint) and its own feed client (long-poll) — dual delivery is idempotent via the atomic signal claim. |

Restart loops (`scripts/start_*.bat`) relaunch their process 5s after
any nonzero exit. The watchdog exits 1 on a 5-minute hang; the
auto-updater exits 77 when code it executes changed — or when the
mode slider switches the consumer's trading mode.

External services:

- **Wealthsimple** (GraphQL, consumer app): account values, positions,
  option chains, quotes, activities, and — in live mode — real orders,
  all under the consumer's own login (`ws_tokens.env` from
  `scripts/ws_login.py`). The pipeline uses allowlisted GraphQL
  documents extracted from the WS app bundle
  (`consumer/ws/ws_positions_query.py`, `ws_security_query.py`) plus the
  `wealthsimple-python` client. All HTTP calls get a hard 15s timeout
  (`ws/ws_http.py`).
- **moomoo / OpenD** (optional, consumer app): realtime option/index
  quotes for the stop monitor and the SPX ladder. Read-only; orders
  never go here.
- **Discord webhooks**: per-consumer trade notifications, alert
  embeds, log lines, update notices, raw-alert feed (info server).
- **GitHub**: the private repo. The info server pulls git (the
  reader restarts off that pull); the consumer follows the rolling
  `consumer-latest` Release (rebuilt by CI on every push to `main`).

### Supervised background threads

All wrapped by `core/ops/supervise.py` (crash → log + Discord notice
+ 30s retry):

| thread | module | runs on | job |
|---|---|---|---|
| alert fan-out | `info/fanout.py` | info | pushes every new signal to each consumer's `/alert` endpoint (bounded retries; pull backfills) |
| stop monitor | `trading/stops.py` | consumer | quote-driven protection for open positions (see [Safety systems](#safety-systems)) |
| trade mirror | `trading/mirror.py` | consumer | books real fills from the WS activity feed, reconciles estimated live bookings, sweeps stale pending orders |
| feed client | `consumer/feedclient.py` | consumer | long-polls the info server's feed, feeds `process_alert`, syncs the SPX levels text |
| auto-updater | `ops/updater.py` + `ops/release_updater.py` | info + consumer | info server: fetch + ff-only pull every interval, restart only when its code changed (the reader restarts off the same pull); consumer: poll the rolling GitHub Release, verify + stage the newer build |
| health watchdog | `ops/watchdog.py` | both | self-fetches `/health`; exits 1 after 5 min of failure → restart loop recovers |
| webhook batcher | `ops/loghook.py` | both | tees stdout/stderr log lines to Discord in 3s batches |

## Alert flow end-to-end

1. **Discord client** — the alert author posts in an allowed channel.
   Discord must run with `--force-renderer-accessibility`
   (`scripts/start_discord.bat`) so its UI tree is exposed.
2. **reader** — UIA poll loop (default 0.5s) reads on-screen messages
   (auto-scrolled to bottom — Discord only exposes scrolled-in rows),
   strips chrome/timestamps/reactions, dedupes via a persisted seen-set
   (`.reader_seen.json` in the reader folder) + reaction-prefix detection, filters by the
   `reader.channels` allowlist.
3. **Delivery to the info server** — each new message is POSTed to
   `reader.pipeline_url` (default `http://localhost:8081/alert`) as
   JSON `{text, author, ts, parsed_ts, channel}` with header
   `X-Auth-Token: <reader.auth_token>` (must match the info server's
   `info.auth_token`). Messages count as seen only after a 2xx;
   failed posts retry from a pending queue.
4. **Info server ingest** — `info/ingest.py: ingest_alert()`: parse
   (`core/parser.py`), dedupe (sha256 message key + reaction-prefix
   check), record into `signals`. The fan-out thread then pushes the
   alert to every registered consumer's `/alert` endpoint (bounded
   retries); consumers that miss the push backfill via their pull
   cursor.
5. **Consumer delivery** — two paths, both active, idempotent (the
   atomic signal claim means only the first delivery executes):
   - **push**: the consumer's own `/alert` route (same shape/auth as
     the reader's) — near-zero added latency;
   - **pull**: the consumer's feed client long-polls
     `GET /api/feed?since=<cursor>&wait=25` — works from anywhere, no
     consumer reachability needed; also syncs the SPX levels text.
6. **Consumer pipeline** — `process_alert()`: risk gates
   ([Safety systems](#safety-systems)) → branch by the consumer's
   trading mode (notify / paper / live — see
   [Trading modes](#trading-modes)) → executor → local store →
   **Discord** result embeds to the consumer's own webhook.
7. **Stop monitor (consumer)** — every `trading.stop_check_seconds`,
   prices each open option position and fires exits through the
   executor.
8. **Dashboards** — the info dashboard polls feed/reader status; each
   consumer's dashboard polls its own `GET /api/dashboard` every ~5s.

## Trading modes

The consumer's `trading.mode` selects its execution path — switched
live from the dashboard's **mode slider** (persist + app restart; the
info server and other consumers are unaffected). `paper.enabled: true`
adds paper execution *alongside* notify mode (phone notifications +
simulated fills). Paper accounts are seeded once from the live value
and positions (`seed_paper_accounts`); `paper.mirror: true` copies
your own real fills into the paper ledger at actual prices.

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

Two dashboards from per-app static asset sets (`consumer/static/`,
`info/static/`),
served by Flask. Auth: username+password sessions (`users` table,
pbkdf2; first boot seeds the admin account from the access token) or
the `X-Auth-Token` machine token for scripts and the reader.
5-fail/15-minute login lockout.

**Consumer dashboard** (`role: consumer`) — the full trading UI:

- **header badges**: mode (pulses red in live), paper, reader state,
  git commit, stops state; **currency toggle**; **eye mask**.
- **mode slider**: notify / paper / live — warns, persists, restarts
  the app (typed `LIVE` confirmation for live).
- **account cards**: real card (value, margin requirement +
  breakdown, open risk, realized today, cluster flag, per-account
  cap) and paper cards seeded from live.
- **open positions table** (options) with per-row tp/trailing guard
  button and a manual sell button (live: real order through the
  executor; greyed out outside live mode); **stock holdings** toggle.
- **paper account ⚙ settings**: reset (drop + re-seed from live),
  resize (past stock trades up to tier sizing), and **adjust** — set
  the cash pools (cad + usd) and edit/remove/add holdings to match
  reality (e.g. after manual trades on the real account).
- **recent alerts** (with ignored-chatter filter) and **trade log**.
- **history search**: merged alert/trade stream, ticker/action/status/
  mode/date filters, free text.
- **SPX levels ladder**: realtime SPX/SPY spot + the levels text
  synced from the info server's feed (read-only there — the editor
  lives on the info dashboard).
- **settings modal**: everything from [Configuration
  reference](#configuration-reference) marked "editable" — applied and
  persisted to the role's config atomically; help text renders under each
  field on touch screens. Accounts are managed here too: add new ones
  (label + id + type), toggle/remove existing, per-account overrides.
- **users panel** (admin): create/delete users, change passwords.

**Info dashboard** (`role: info`) — the lean source-of-truth page:
consumer health table (feed last-seen, cursor, push stats), the
recent alert feed, the reader line, and the SPX levels editor (the
cross-device source of truth). **No login** — it serves no session
cookie (so it can't fight the consumer app's cookie on the same
host). The write routes are token-guarded with the reader's
`X-Auth-Token` (no cookie): `POST /alert` (a fake alert would make
consumers trade) and the levels + settings POSTs (a fake levels
text reaches every consumer's ladder and informs trades just the
same) — the dashboard prompts once for the token and keeps it in
localStorage.

## HTTP API reference

All API paths require auth (browser session or `X-Auth-Token` header)
except `/health`, `/favicon.ico`, and `/login`, plus the info role's
open read-only GETs (`/api/feed-status`, `/api/levels`,
`/api/reader_status`, `/api/settings`, `/api/signals` — the info
dashboard has no login). Admin-gated routes check the session role.

| method | path | auth | purpose |
|---|---|---|---|
| GET/POST | `/login` | none | login page / credential check; 5-fail/15-min lockout |
| GET | `/logout` | session | clear session, redirect |
| GET | `/` | session | dashboard page (lean info page when `role: info`) |
| GET | `/health` | none | liveness `{"status":"ok"}` (watchdog target) |
| GET | `/api/feed` | consumer token | **info role** — alerts after the `since=` cursor (long-poll `wait=` up to 25s), SPX levels text, new cursor; without `since=`: head-only (fresh consumers anchor here) |
| GET | `/api/feed-status` | open | **info role** — per-consumer health (last seen, cursor, pushed/failed, last error) |
| GET | `/api/summary` | any | account cards summary (mode, values, margin, risk, reader, stops) |
| GET | `/api/dashboard` | any | one batched poll: summary + paper positions + positions + signals + trades + settings + update status + me (cached, stale-while-revalidate) |
| GET | `/api/positions` | any | open positions (live WS rows where fetchable, ledger rows otherwise) |
| GET | `/api/paper-positions` | any | paper holdings with live pricing and pnl per account |
| POST | `/api/paper-reset` | admin | reset a paper account's ledger and re-seed from live |
| POST | `/api/paper-resize` | admin | bring past paper STOCK positions up to tier sizing |
| POST | `/api/paper-sell` | admin | manual close of a paper position at its live price (whole or given qty) |
| POST | `/api/paper-adjust` | admin | the paper account's adjust editor: set the cash pools (cad + usd) and replace the holdings (edit qty/avg, remove, add) |
| POST | `/api/position-sell` | admin | live mode: manual REAL sell of a tracked live position at the current bid (stop-monitor path) |
| POST | `/api/position-tp` | admin | set/clear per-position take-profit (`tp_gain_pct`) and trailing (`trail_pct`) guards |
| GET | `/api/signals?limit=` | any | recent recorded signals |
| GET | `/api/trades?limit=` | any | recent trade log |
| GET | `/api/history` | any | search retained history (kind, ticker/action/status/mode, since/until, q, limit/offset) |
| GET | `/api/spx` | any | realtime SPX/SPY spot for the ladder (moomoo or WS, SPY×10.0391 proxy fallback) + the feed-synced levels text (read-only — the info server owns it) |
| GET | `/api/settings` | any | the dashboard-editable settings view (incl. the current mode) |
| POST | `/api/settings` | admin | validate + apply + persist settings (no restart) |
| POST | `/api/mode` | admin | the mode slider: validate + persist `trading.mode` + restart the app |
| GET | `/api/update_status` | any | auto-updater status (last check/result/commit/branch) |
| POST | `/api/reader_status` | token | reader heartbeat (`{channel, ok}`); returns `{}` — reader settings live in `reader/reader.config.yaml` (an edit applies on the reader's restart) |
| GET | `/api/reader_status` | any | current reader heartbeat state (dashboard reader line) |
| GET | `/api/users` | admin | list users (no hashes) |
| POST | `/api/users` | admin | create/delete/set_password (self-change needs current password; cannot delete self or last admin) |
| POST | `/alert` | token | **alert ingest** — JSON `{text, author, ts, parsed_ts, channel}`; runs `process_alert` (consumer) / `ingest_alert` (info) |
| GET | `/api/levels` | open | **info role** — the current SPX levels text (read-only) |
| POST | `/api/spx-levels` | token | **info role** — save the levels text (consumers render it read-only on their ladder) |

The seven trading routes (`/api/positions`, `/api/paper-*`,
`/api/position-*`) return 404 on an info server — trading runs on
consumer apps.

## Database

SQLite (`<role>.trades.db` — `info.trades.db`, `consumer.trades.db`),
WAL, per-thread connections, writes serialized,
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

Backups: `sqlite3 consumer/consumer.trades.db ".backup backup.db"`
while running (a raw
copy can miss WAL contents). Clean slate: `scripts/clean_start.py`
(wipes the db's signals/trades + that role's log files only —
never the other roles' logs).

## Configuration reference

Full commented templates per role in `config/`:
`config/consumer.config.yaml`, `config/info.config.yaml`,
`config/reader.config.yaml`. Dashboard-editable
settings are applied live via `/api/settings` and persisted to
the role's config; the rest needs a restart.

### `consumer` / `info` — role + web server
The web-server section is named for the role (`consumer:` in the
consumer config, `info:` in the info config — the section key IS
the role; a config with neither defaults to consumer):
`host` (0.0.0.0), `port` (8080 default), `auth_token` (**required** —
the server refuses to start without it; it seeds the admin account
and authenticates the reader and scripts),
`tls_cert`/`tls_key` (empty = plain HTTP).

### `consumers` — info role: who may read the feed
`consumers[]`: `label`, `token` (authenticates both directions —
their feed client against this server, this server's push against
their `/alert`), `push_url` (empty = pull-only consumer).

### `feed` — consumer role: where the feed lives
`url` (the info server), `token` (must match a `consumers[]` entry),
`poll_seconds` (1.0), `verify_ssl` (false — the info server usually
runs a self-signed cert).

### `trading` — mode + sizing + risk
`mode` (notify|paper|live — also the **mode slider** in the
dashboard, which persists + restarts), `order_type` (limit|market),
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
`accounts[]` ships **blank** — add accounts from the dashboard's
settings (or the config). Each entry: `account_id`, `label` (fixed
once set — it keys the account's ledger), `type` (`margin` — borrows
against holdings; `non_margin` — registered plans (RRSP, TFSA, FHSA
model as non_margin: cash covers the position, margin exempt); empty
= auto-detect from the Wealthsimple account type), `enabled`, and
per-account
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
the info server's `info.auth_token`**),
`max_items` (40),
`auto_scroll`, `auto_start_discord`, `auto_switch_channel`,
`channel_servers` map, `discord_reopen_seconds`
(15), `discord_restart_seconds` (90), `discord_start_command`,
`channels` allowlist (**never** include your webhook output channel;
the first entry is the channel the reader sits in).
Reader knobs are edited in `reader/reader.config.yaml` directly — the
reader watches the file's mtime and restarts to apply an edit.

### `discord` — webhooks
`notify` (master switch), `trade_alert_webhook_url` (trade alerts to
your phone), `consumer_log_webhook_url` (this app's log tail),
`update_webhook_url` (empty = off — no fallback webhook anywhere).
The reader's webhooks
(`reader_log_webhook_url`, `raw_alert_webhook_url`,
`update_webhook_url`) live in `reader/reader.config.yaml`; the info
server only uses `consumer_log_webhook_url` + `update_webhook_url`.

### `auto_update`
`enabled` (true), `interval_seconds` (600). The updater is chosen by
role: the **info server** (git checkout) does `git fetch` + ff-only
pull and restarts (exit 77) only when its code changed — the reader
runs no updater and restarts off that same pull; the **consumer**
follows the rolling GitHub Release (`release_tag`,
`consumer-latest`): it polls, downloads a newer `consumer-<commit>.zip`
asset, verifies its SHA256 and stages the swap — the launcher applies
it on the next restart (a git-checkout consumer seeds its VERSION
marker from the checkout on the first check). Private repos need
`github_token` (a read-only token) or the `GITHUB_TOKEN` env var.

### `parser`
`custom_patterns` ([]) — extra regexes for alert formats the built-in
parser misses.

## Deployment

### The owner's box (Windows) — reader + info server + your consumer app

Each role lives in its **own folder** with its own
`<role>.config.yaml` and `<role>.trades.db`; the launchers live in
`scripts/` and every log
lands in the shared `logs/` folder:

```
ws-trade-bot/
  info/        info.config.yaml · info.trades.db
  consumer/    consumer.config.yaml · consumer.trades.db
  reader/      the Discord watcher (own venv)
  scripts/     start_info.bat · start_consumer.bat · the rest
  logs/        info.log · consumer.log · reader.log
```

1. **Clone** the repo (or run
   `scripts/setup_info_server_git_pull.ps1` to set up the
   SSH deploy key for the info server's unattended auto-update
   pulls — consumer-only machines don't need it, they follow the
   GitHub release channel with a token instead).
2. **Role configs** (once): copy `config/info.config.yaml` to
   `info/info.config.yaml` (port 8081 is already set — add an
   `auth_token` and register consumers under `consumers[]`) and
   `config/consumer.config.yaml` to `consumer/consumer.config.yaml`
   (port 8080 is already set — add an `auth_token`, a `feed:`
   section pointing at the info server, and your
   `wealthsimple.accounts[]`).
3. **Info server**: `scripts\start_info.bat` — the reader keeps posting to
   `:8081` unchanged.
4. **Your consumer app**: `scripts\start_consumer.bat` — dashboard on
   `http://127.0.0.1:8080` (expose via Tailscale for phone access).
5. **Wealthsimple login** (consumer): `python scripts/ws_login.py` —
   stores tokens in the Windows keyring (primary) plus a gitignored
   `consumer/ws_tokens.env` fallback, and prints account IDs for
   the consumer config.
6. **Reader**: `scripts/start_reader.bat` (own venv) and
   `scripts/start_discord.bat` (Discord with
   `--force-renderer-accessibility`).
7. **HTTPS + remote access**: Tailscale Serve proxies each
   dashboard with a valid tailnet cert — no cert management:
   `tailscale serve --bg http://127.0.0.1:8080` (consumer, on
   443) and, on a second port, `tailscale serve --bg
   --https=8443 http://127.0.0.1:8081` (info). 
   `scripts/gen_cert.py` provides self-signed certs for the
   apps' own TLS mode instead.
8. **moomoo (optional)**: install OpenD on a consumer machine and set
   `quotes.provider: moomoo` there.
9. **Client releases**: CI (`.github/workflows/release.yml`) rebuilds
   the rolling `consumer-latest` Release on every push to `main` —
   the `consumer-<commit>.zip` + `SHA256SUMS` assets other users'
   release installs self-update from. Build one locally with
   `python scripts/build_consumer_zip.py`.

### Another user's consumer app (any machine)

Two install types — both auto-update:

**Release install (recommended for other users)** — no git needed:

1. Download the newest `consumer-<commit>.zip` from the repo's
   GitHub Releases (`consumer-latest`) and unzip it to a folder.
2. Run `scripts\install_consumer.bat` once — creates the venv,
   installs dependencies, seeds `consumer/consumer.config.yaml` from the
   example.
3. Edit `consumer/consumer.config.yaml`: a local
   `auth_token`, the `feed:` section (the info server's URL
   + their consumer token), and their `wealthsimple.accounts[]`.
   While the repo is private also set
   `auto_update.github_token` (a read-only token) — the client uses
   it to poll and download new release assets.
4. `python scripts/ws_login.py` with THEIR Wealthsimple login.
5. Run `scripts\start_consumer.bat` — they get the full dashboard,
   their own paper/live trading, their own webhook alerts. From then
   on the app self-updates: the updater polls the release every
   `auto_update.interval_seconds`, stages a newer build, and the
   launcher applies it on restart (config, tokens, db, logs and
   certs survive every update; the artifact ships no secrets).

**Git install** (like the owner's box): clone the repo, then:

1. `pip install -r requirements.txt` (or run
   `scripts\start_consumer.bat`, which does it).
2. Copy `config/consumer.config.yaml` → `consumer/consumer.config.yaml`; set
   a local `auth_token`, the `feed:` section (the info server's URL + their consumer token),
   and their `wealthsimple.accounts[]`.
3. `python scripts/ws_login.py` with THEIR Wealthsimple login.
4. Run `scripts\start_consumer.bat` — they get the full dashboard,
   their own paper/live trading, their own webhook alerts. Updates
   follow the same release channel as any other consumer install
   (the updater seeds its VERSION marker from the checkout on the
   first check) — no git access needed after the initial clone.

Either way, the owner adds a matching `consumers[]` entry (label +
token) on the info server; add a `push_url` too if the consumer is
reachable (e.g. both on Tailscale).

### Mode switching

The consumer dashboard's **mode slider** (notify / paper / live)
persists `trading.mode` and restarts that consumer app only — with a
warning modal, and a typed `LIVE` confirmation for live mode.

## Development (any OS)

```bash
python -m venv .venv
# windows: .venv\Scripts\python -m pip install -r requirements.txt
.venv/bin/pip install -r requirements.txt
cp config/consumer.config.yaml config.yaml   # keep host 127.0.0.1 for a dev stub
.venv/bin/python run.py -c config.yaml       # windows: .venv\Scripts\python run.py -c config.yaml
```

The venv layout differs per OS: `.venv/bin/` on linux/macOS,
`.venv\Scripts\` on windows — every `.venv/bin/...` command below
becomes `.venv\Scripts\...` there. The pipeline runs fine on Linux
and Windows (the reader does not — it needs the Windows UIA). A dev
config with `mode: notify` + `paper.enabled: true` on 127.0.0.1
exercises the whole pipeline without real orders.

## Testing

Run the tests through pytest from the repo root (`python -m pytest`
puts the repo root on sys.path; invoking a test file directly does
not and fails on the imports). The suite has three modes (pytest
markers, see `pytest.ini`) — minimum ⊂ essential ⊂ full:

```bash
# minimum: smoke - configs load, alerts parse, settings save (11 tests)
.venv/bin/python -m pytest tests -q -m minimum

# essential: + parser, settings, reader, stops, users, feed, mirror,
# dashboard assets (~250 tests)
.venv/bin/python -m pytest tests -q -m essential

# full: everything (~490 tests, stubs for the reader's UIA) - the default
.venv/bin/python -m pytest tests -q --ignore=tests/scripts

# windows: same commands with .venv\Scripts\python (the suite runs
# on any OS; the reader tests stub the UIA)

# full-stack black-box E2E (boots the real server on a temp config/db)
.venv/bin/python tests/scripts/e2e_test.py

# headless-Chrome dashboard smoke test (chrome/edge on PATH or in
# its standard install location, or set CHROME_BIN)
.venv/bin/python tests/scripts/ui_test.py

# JS syntax check after editing the dashboard (needs node)
node --check consumer/static/dashboard.js
```

Key suites: `test_pipeline.py` (end-to-end alert processing),
`test_parser.py`, `test_reader.py`, `test_dashboard.py` (HTML/CSS/JS
invariants), `test_stops.py`, `test_mirror.py`,
`test_live_executor.py` (every live path against a fake ws client,
incl. the kill switch), `test_settings_update.py`, `test_users.py`,
`test_updater.py` (git-pull flow), `test_release_updater.py` +
`test_build_zip.py` (the release client + its artifact),
`test_store_migration.py`.

## Operations

- **restart chain**: watchdog exits 1 after a 5-min hang →
  `start_*.bat` relaunches after 5s; the auto-updater exits 77 when
  server code changed; the **mode slider** exits 77 after persisting;
  `run.py` kills stale copies of itself holding the port
  (`ops/processes.py`) and prints the previous exit code from
  `pipeline_exit.txt`.
- **logs**: one shared folder - `logs/info.log`,
  `logs/consumer.log`, `logs/reader.log` - each optionally
  teed to Discord in 3s batches (`install_log_webhook`).
- **updates**: role-based. Info server: `git fetch` + ff-only pull
  every `auto_update.interval_seconds`, dirty runtime files healed;
  the reader restarts off that pull. Consumer: poll the rolling
  GitHub Release, verify + stage the newer build, the launcher
  applies it on restart. `.last_update.json` records the last
  update; the dashboard shows the running commit/release.
- **known-exit codes**: 77 = code update or mode-switch restart, 1 =
  watchdog hang detection, anything else = crash (restart loop
  catches it).
- **expectancy report** (consumer): `python scripts/expectancy.py` —
  per-tier win rate / avg win / avg loss / expectancy from closed
  paper round trips; drives the "set `contracts_max: 0` on
  negative-expectancy tiers" decision rule.
- **diagnostics**: `python scripts/diagnose.py` (deps, config,
  connectivity); `python reader/inspect_discord.py` (reader
  debugging - dumps the Discord UIA tree).
- **go-live**: see `docs/live_readiness_plan.md` for the checklist,
  suggested starting settings, and the safety-system reference.

## Repository layout

```
run.py                     # entry point: role wiring + threads + server
config/                    # per-role documented config templates
  consumer.config.yaml     # the trading app (copy to consumer/)
  info.config.yaml         # the alert source (copy to info/)
  reader.config.yaml       # the Discord watcher (copy to reader/)
info/                      # the info server's runtime folder
  info.config.yaml         # (gitignored)
  info.trades.db           # signals + users (gitignored)
consumer/                  # the consumer app's runtime folder (one per trader)
  consumer.config.yaml     # (gitignored)
  consumer.trades.db       # full trading state (gitignored)
scripts/
  start_info.bat           # info launcher + restart loop
  start_consumer.bat       # consumer launcher + restart loop (+ applies staged release updates)
  install_consumer.bat     # first-time release-install setup
  apply_update.py          # applies a staged release update (called by the launcher)
  build_consumer_zip.py    # builds the consumer release artifact
reader/
  discord_reader.py        # UIA Discord watcher (standalone, Windows)
  inspect_discord.py       # Discord window utilities + CLI diagnostic
  requirements-windows.txt
core/                      # shared foundation (both apps execute this)
  config.py                # YAML → dataclass config (load_config)
  store.py                 # SQLite persistence + migrations
  parser.py                # alert regexes, Alert dataclass, contract keys
  signals.py               # message keys + the atomic signal claim
  web_common.py            # session auth, login page, gzip, log quieting
  ops/
    notify.py loghook.py processes.py supervise.py updater.py watchdog.py
    release_updater.py     # release-install updater (GitHub Releases)
info/                      # the alert source app (code + runtime + launcher)
  server.py                # wiring: store, fan-out, updater, watchdog, wsgi
  web.py                   # Flask app: ingest, feed, reader heartbeat, levels
  ingest.py                # parse + dedupe + record (no execution)
  fanout.py                # push alerts to consumers' /alert endpoints
  dashboard.py             # lean info page assets
  static/info.{html,js}
consumer/                  # the trading app (code + runtime + launcher)
  app.py                   # wiring: executors, stops, mirror, feed client
  web.py                   # Flask app: dashboard API, settings, mode, users
  pipeline.py              # process_alert: parse → dedupe → risk → execute
  settings.py              # settings get/apply/persist + set_mode
  feedclient.py            # long-poll the info server's alert feed
  dashboard.py             # trading dashboard assets
  static/dashboard.{html,css,js}
  trading/
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
scripts/                   # ws_login, gen_cert, expectancy, diagnose,
                           # clean_start, setup_info_server_git_pull,
                           # start_info/start_consumer/start_reader/
                           # start_discord .bat
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
| `docs/test_plan.md` | test plan: automated coverage map + manual/E2E procedures for the updater, release client, file naming, and accounts |

Re-render the diagrams after editing the `.dot` sources:

```bash
dot -Tpng -o docs/architecture.png docs/architecture.dot
dot -Tsvg -o docs/architecture.svg docs/architecture.dot
# (same for runtime.dot and db_schema.dot)
```

## Roadmap

Open ideas, in no particular order:

1. **Consumer management UI** — the info server's consumers list is
   config-file managed; add create/revoke + token rotation to the
   info dashboard.
2. **Enforce username+password login** — remove the bare-token browser
   fallback: the access token stays as the machine credential for the
   reader/scripts and seeds the admin on upgrade, but can no longer
   open a browser session on its own. Change-password form gets a
   new+confirm pair with a mismatch check.
3. **Feed push over TLS** — the fan-out verifies consumer certs only
   when `push_verify_ssl` is set; consider a proper CA or Tailscale
   cert guidance for consumer endpoints.
4. **Keep diagrams current** — re-render the dot sources when the
   schema, architecture, or runtime layout change.
