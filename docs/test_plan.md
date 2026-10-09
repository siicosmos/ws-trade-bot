# Test plan

Covers the release-install client, the role-split updater, the
per-role file naming, and the account model. Each area lists the
automated coverage that already exists (pytest), the exact commands
to run it, and the manual procedures that need a real machine,
real network, or real Discord/Wealthsimple.

Run the automated suite with:

```bash
# full suite (default)
.venv/bin/python -m pytest tests -q --ignore=tests/scripts
# or scoped by mode (pytest markers, see pytest.ini):
.venv/bin/python -m pytest tests -q -m minimum     # smoke
.venv/bin/python -m pytest tests -q -m essential   # core behavior
```

On windows the interpreter lives at `.venv\Scripts\python.exe`
(every `.venv/bin/...` command above becomes
`.venv\Scripts\...`). Always run through `python -m pytest` from
the repo root - invoking a test file directly fails on the imports.

minimum ⊂ essential ⊂ full: the minimum set proves the install is
alive (configs load, alerts parse, settings save); essential adds
the core behavior modules (parser, settings, reader, stops, users,
feed, mirror, dashboard assets); everything else - the pipeline
integration, updater, history search, launcher/watchdog edge cases -
is full-only.

The suite is fully green on any OS (the reader tests stub the
Windows UIA); it needs `requirements.txt` installed
(`wealthsimple-python` backs the ws-http shim test).

Conventions for every manual step below:

- `<info-token>` = `info.auth_token` from `config/info.config.yaml`
  (also the reader's credential and the info write-route token).
- `<consumer-token>` = `consumer.auth_token` from
  `config/consumer.config.yaml` (seeds the dashboard's admin).
- `curl.exe` ships with windows 10+; `-s` silences progress,
  `-o NUL -w "%{http_code}"` prints just the status code.

---

## 1. Config system

**Automated** (`tests/test_config_example.py`)

- `consumer:` / `info:` section keys load; the section key IS the
  role (no `role:` field needed). A config with neither section
  defaults to the consumer role with default host/port.
- Templates parse, load through `load_config`, carry the right
  role/port defaults, ship no secrets.
- Reader template is flat-key; the removed keys
  (`channel_marker`, `discord_server`) stay out of it.
- Account `type` normalized (`MARGIN` → `margin`, unknown → `""`).

```bash
.venv/bin/python -m pytest tests/test_config_example.py -q
```

**Manual**

#### M-1.1 — Start each role once from its config (`config/consumer.config.yaml`, `config/info.config.yaml`) and confirm the dashboard + role dispatch work.

**Steps:**

1. Start the info server: `scripts\start_info.bat`.
2. Role dispatch picked info — `logs\info.log` opens with
   `info server starting - commit <sha>` (NOT "consumer app"):
   ```powershell
   Get-Content logs\info.log -Tail 3
   ```
3. The lean info dashboard serves (consumer health table,
   reader line, levels editor — no mode slider, no account
   cards): open `http://127.0.0.1:8081/`.
4. Liveness + open reads:
   ```powershell
   curl.exe -s http://127.0.0.1:8081/health
   # -> {"status":"ok"}
   curl.exe -s http://127.0.0.1:8081/api/feed-status
   # -> per-consumer health JSON (open GET)
   ```
5. No trading wiring on the info role — the trading routes
   do not exist here:
   ```powershell
   curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8081/api/positions
   # -> 404
   ```
6. Start the consumer: `scripts\start_consumer.bat`.
7. Role dispatch picked consumer — `logs\consumer.log`
   opens with `consumer app starting - commit <sha>` and the
   mode line (`running in PAPER mode on ...`).
8. The full trading dashboard serves (mode slider, account
   cards, trade log): open `http://127.0.0.1:8080/` — it
   redirects to `/login`; log in as `admin` with the
   consumer's `auth_token` (the token seeded the admin).
9. Machine-token API access:
   ```powershell
   curl.exe -s -H "X-Auth-Token: <consumer-token>" http://127.0.0.1:8080/api/summary
   # -> JSON with mode / accounts[] / reader
   ```
10. The dispatch did not cross wires: `http://127.0.0.1:8081/`
    shows no trading UI, and
    `curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8080/api/feed-status`
    → 404 (that route is info-only).

## 2. Per-role file names

**Automated**

- `test_release_updater.py` apply test — state files survive the
  release swap.

```bash
.venv/bin/python -m pytest tests/test_release_updater.py -q -k "apply"
```

**Manual**

#### M-2.1 — On the live box: stop both apps, confirm the per-role dbs exist, restart via the `.bat`s, and confirm the dashboards still show full history (no fresh empty db).

**Steps:**

1. Stop both roles — killing the python processes alone is
   not enough (the launcher loops restart them in 5s); kill
   each launcher tree (cmd + its python child):
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
     Where-Object { $_.CommandLine -match "start_consumer.bat|start_info.bat" } |
     ForEach-Object { taskkill /F /T /PID $_.ProcessId }
   ```
   (Closing the launcher console windows works too — it
   kills the attached python.) Verify nothing is listening:
   ```powershell
   curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8081/health
   # -> 000 (connection refused)
   ```
2. The per-role dbs exist at the role paths:
   ```powershell
   Get-ChildItem consumer\consumer.trades.db, info\info.trades.db
   ```
3. Restart via `scripts\start_info.bat` +
   `scripts\start_consumer.bat`.
4. History survived: log into
   `http://127.0.0.1:8080/` — the trade log / recent alerts
   show the pre-restart rows (a fresh db would show none).
   Row counts straight from sqlite:
   ```powershell
   & .venv\Scripts\python -c "import sqlite3; print(sqlite3.connect(r'consumer\consumer.trades.db').execute('select count(*) from trades').fetchone())"
   ```

## 3. Updater routing (role-based)

**Automated** (`tests/test_release_updater.py`)

- consumer role → `ReleaseUpdater` even inside a git checkout;
  info role → `AutoUpdater`.
- Release updater seeds `VERSION` from the git checkout (head +
  origin slug) on the first check, then compares releases.
- `update_status_payload` works for both updater types.

```bash
.venv/bin/python -m pytest tests/test_release_updater.py -q -k "role or seed or routing"
.venv/bin/python -m pytest tests/test_roles.py -q -k "updater"
```

**Manual**

#### M-3.1 — Consumer (git checkout, live box): start it, watch `consumer.log` for the release seeding and the absence of git operations.

**Steps:**

1. `scripts\start_consumer.bat`.
2. ```powershell
   Select-String "release channel|up to date|git fetch" logs\consumer.log | Select-Object -Last 5
   ```
   Expect `release channel seeded from the git checkout
   (<sha>)` then `up to date` — and NO `git fetch` /
   dirty-tree lines (a git-checkout consumer must not run
   git itself).
3. The seeded marker matches the checkout:
   ```powershell
   Get-Content VERSION   # the repo root, next to consumer\
   git rev-parse --short HEAD
   ```

#### M-3.2 — Info server: confirm it still logs the git-poll cycle and pulls on a new push to main.

**Steps:**

1. `scripts\start_info.bat`, then push any commit to main.
2. ```powershell
   Select-String "auto-update" logs\info.log | Select-Object -Last 5
   ```
   Expect `auto-update: updated to <sha>:` followed by the
   commit list (or `up to date` before the push lands).

## 4. Release updater (consumer client)

**Automated** (`tests/test_release_updater.py`)

- Version compare (up to date / newer), no-release, no-asset,
  disabled, pending-guard (staged update waits for the launcher).
- Download + SHA256 verify; checksum mismatch aborts with no
  staging and no restart.
- Staging extracts `core/` + `consumer/`, writes
  `.update_pending.json`, restarts once.
- `apply_update`: code dirs swapped, state files
  (`consumer.config.yaml`, `ws_tokens.env`, `consumer.trades.db*`)
  preserved, `VERSION` + `.last_update.json` written, staging +
  marker cleaned, `.bat` files never swapped.
- `apply_update` failure path: a swap that dies mid-way restores
  the state files (consumer/ recreated if needed) and leaves the
  pending marker + staging for the launcher to retry.
- A truncated/wrong artifact (no `core/`/`consumer/` inside) is
  detected and aborted — no staging, no restart.

```bash
.venv/bin/python -m pytest tests/test_release_updater.py -q
.venv/bin/python -m pytest tests/test_build_zip.py -q
```

**Manual (real network + real release)**

#### M-4.1 — End-to-end self-update: push a trivial commit to main → CI publishes `consumer-latest` → the consumer self-updates. Confirm config/db/webhooks survived.

**Steps:**

1. Push a trivial commit (`touch docs/x.md`) to main and
   wait for the CI release workflow to go green.
2. On the consumer box, within `auto_update.interval_seconds`:
   ```powershell
   Select-String "staged|restarting to apply|applied" logs\consumer.log | Select-Object -Last 5
   # expect: auto-update: staged <sha> - restarting to apply...
   #         apply_update: applied <sha>
   ```
3. The swap preserved the state:
   ```powershell
   Get-ChildItem consumer\consumer.config.yaml, consumer\ws_tokens.env, consumer\consumer.trades.db
   Get-Content VERSION                    # -> the new sha (repo root)
   Get-Content .last_update.json | ConvertFrom-Json
   ```
4. The dashboard still works (M-1.1 steps 8-9) and the
   trade log still shows history (M-2.1 step 4).

#### M-4.2 — Checksum tamper: confirm the published zip matches its SHA256SUMS (the tamper-abort path itself is automated).

**Steps:**

1. Download `consumer-<sha>.zip` + `SHA256SUMS` from the
   GitHub release.
2. ```powershell
   Get-FileHash consumer-<sha>.zip -Algorithm SHA256
   # compare against the hash line in SHA256SUMS
   ```

#### M-4.3 — Private-repo token: with `github_token` unset, the poll fails gracefully; with a read-only token set it succeeds.

**Steps:**

1. Comment out `auto_update.github_token` in
   `config/consumer.config.yaml`, restart.
2. ```powershell
   Select-String "release check failed" logs\consumer.log | Select-Object -Last 2
   # expect: release check failed: 401 / 404 - and the app keeps running
   ```
3. Restore the token, restart, confirm `up to date`.

#### M-4.4 — Poll-during-publish race: while CI recreates the release, a poll may see a 404 — confirm it logs and retries next cycle without restarting.

**Steps:**

1. Push a commit and watch `logs\consumer.log` during the
   CI window.
2. A transient `release check failed: 404` line must be
   followed by a normal cycle (no restart, no crash).

## 5. Git updater (info server) + restart scoping

**Automated** (`tests/test_updater.py`, `tests/test_pipeline.py`,
`tests/test_release_updater.py`)

- Fetch + ff-only pull, restart only when role code changed,
  untracked files never block, dirty tracked files block,
  ignored runtime files are healed, stale `index.lock` cleared,
  update record roundtrip, interval change applies mid-cycle.
- Webhook titles are role-branded: `Info server restarting` /
  `Consumer app restarting` / `... updated (no restart)`.

Run them directly:

```bash
# the whole updater suite (pull flow, restart scoping, record)
.venv/bin/python -m pytest tests/test_updater.py tests/test_release_updater.py -q

# just the pull/restart scoping + the update record
.venv/bin/python -m pytest tests/test_updater.py -q -k "check_once or record or seed"

# the startup banner's "(updated N ago via pull)" annotation
.venv/bin/python -m pytest tests/test_pipeline.py -q -k "banner"

# the release-client swap (consumer) incl. the record write
.venv/bin/python -m pytest tests/test_release_updater.py -q -k "apply"
```

**Manual (live info server)**

#### M-5.1 — Push a docs-only commit: info server pulls, posts "Info server updated (no restart)", keeps running. Then verify the update record followed the pull:

```powershell
Get-Content .last_update.json | ConvertFrom-Json
# "commit" must equal the current head:
git rev-parse --short HEAD
# "how" is "auto" and "ts" is within the last interval
```

(Regression this guards: the no-restart pull path used to
skip the record write, leaving the hash pointing at the old
commit - `test_check_once_records_no_restart_pull`.)

#### M-5.2 — Push an `info/*` commit: pulls, posts "Info server restarting", restarts, banner shows the new commit. Confirm in `logs\info.log`:

```powershell
Select-String "auto-update|starting" logs\info.log | Select-Object -Last 5
# expect: auto-update: updated to <sha>: ... /
#         auto-update: restarting pipeline... /
#         info server starting - commit <sha> (auto-updated ...)
```

#### M-5.3 — Discord: confirm the embed titles match the role (no more "Pipeline restarting") for info + consumer.

**Steps:**

1. Push a docs-only commit → the Discord webhook channel
   shows `Info server updated (no restart)`.
2. Push an `info/*` commit → `Info server restarting`.
3. Push a `consumer/*` commit → the consumer's webhook
   shows `Consumer app restarting` (mode slider restarts
   also post there).

#### M-5.4 — Info dashboard write routes are token-guarded (automated in `test_roles.py::test_info_write_routes_are_token_guarded`).

**Steps:**

1. Negative (no token):
   ```powershell
   curl.exe -s -o NUL -w "%{http_code}" -X POST http://127.0.0.1:8081/api/spx-levels -H "Content-Type: application/json" -d "{\"text\":\"x\"}"
   # -> 401
   ```
2. Positive (reader token):
   ```powershell
   curl.exe -s -X POST http://127.0.0.1:8081/api/spx-levels -H "X-Auth-Token: <info-token>" -H "Content-Type: application/json" -d "{\"text\":\"SPX 6000/6050 credit spread\"}"
   # -> {"status":"ok"} - and the ladder text shows on every consumer
   ```
3. In the browser: the levels editor prompts once for the
   token (localStorage, no cookie); a wrong token gets a
   401, is dropped, and the next save re-prompts. The
   read-only GETs stay open.

#### M-5.5 — Dashboard git badge: open the info dashboard and confirm the badge shows the current head and the `last_pull` timestamp matches the record (`Get-Content .last_update.json`).


## 6. Reader (rides the info pull)

**Automated**: reader logic is Windows/UIA-bound — covered only
by stubs (`test_reader.py` timing tests).

```bash
.venv/bin/python -m pytest tests/test_reader.py -q
.venv/bin/python -m pytest tests/test_scroll_fallback.py -q
```

**Manual (Windows box)**

#### M-6.1 — Push a `reader/*` commit → info server pulls → the reader restarts into the new code.

**Steps:**

1. Push a `reader/*` commit; wait for the info server's
   pull cycle.
2. ```powershell
   Select-String "repo updated|Reader restarting" logs\reader.log | Select-Object -Last 3
   # expect: repo updated on disk - restarting reader for ...
   #         Reader restarting (webhook embed)
   ```
3. The reader comes back watching Discord:
   `watching channel: '...'` reappears, and the info
   dashboard's reader line goes green.

#### M-6.2 — Push a docs-only commit → the reader stays up.

**Steps:**

1. Push a docs-only commit; wait one reader cycle.
2. ```powershell
   Select-String "repo updated" logs\reader.log | Select-Object -Last 2
   # expect: repo updated (no reader changes) - staying up
   ```

#### M-6.3 — Edit `config/reader.config.yaml` while running → reader restarts to apply it.

**Steps:**

1. Edit any value (e.g. `poll_interval: 0.6`) and save.
2. ```powershell
   Select-String "changed - restarting" logs\reader.log | Select-Object -Last 2
   # expect: reader.config.yaml changed - restarting to apply
   ```
3. The startup banner re-logs the new value
   (`poll every 0.6s`).

## 7. Accounts (add/remove, margin model)

**Automated** (`tests/test_settings_update.py`,
`test_config_example.py`)

```bash
.venv/bin/python -m pytest tests/test_settings_update.py -q -k "account"
.venv/bin/python -m pytest tests/test_config_example.py -q -k "account"
```

- Blank config → add two accounts (margin + non_margin) →
  persisted with types; remove one; flip type back to auto.
- Validation: bad type reported, missing label rejected,
  unknown label = add, known label = update.
- `account_is_non_margin`: explicit type wins over API type and
  label keywords; API type next; label keyword last.
- **new accounts get paper accounts**: adding a real account and
  re-running the seed gives the new label a paper account seeded
  from its live value; existing accounts' ledgers are untouched
  (the paper_seed marker guards them).
- **paper adjust**: the `/api/paper-adjust` endpoint sets both
  cash pools (cad + usd) and replaces the holdings (stock +
  option rows); bad numbers are reported without crashing; the
  ledger's valuation includes the usd cash pool.
- Consumer example ships `accounts: []`.

**Manual (dashboard + real WS)**

Account edits go through the browser's settings modal — the
settings POST replaces the whole `accounts[]` list, so do NOT
hand-craft partial account POSTs against the live box. Use curl
only for the read-only checks shown.

#### M-7.1 — Settings modal: add an account row (label, id, type=non_margin), save, restart, confirm the account card appears and sizing uses it.

**Steps:**

1. Dashboard → settings → accounts → + add: label, account
   id (from `scripts\ws_login.py` output), type
   `non_margin`. Save (persists to
   `config/consumer.config.yaml`).
2. Restart the consumer (`scripts\start_consumer.bat`).
3. The card appears:
   ```powershell
   curl.exe -s -H "X-Auth-Token: <consumer-token>" http://127.0.0.1:8080/api/summary
   # accounts[] carries the new label
   ```
4. The config gained the row:
   ```powershell
   Select-String "account_id" consumer\consumer.config.yaml
   ```

#### M-7.2 — Remove an account, save, restart — card gone, ledger rows still in the db.

**Steps:**

1. Settings → remove the test account → save → restart.
2. `GET /api/summary` no longer lists it.
3. The ledger rows survive:
   ```powershell
   & .venv\Scripts\python -c "import sqlite3; print(sqlite3.connect(r'consumer\consumer.trades.db').execute('select distinct account from positions').fetchall())"
   ```

#### M-7.3 — Margin behavior: the margin account shows a real margin breakdown; the RRSP (non_margin) suppresses it — also confirm an explicit `type` overrides what the WS API reports (set RRSP to `margin` temporarily and watch the card flip, then set it back).

**Steps:**

1. `GET /api/summary` → the margin account carries
   `margin_requirement` + `margin_breakdown`; the
   non_margin one carries `null`.
2. Settings: flip the RRSP's type to `margin`, save,
   restart → the RRSP card now shows a margin requirement.
   Flip it back.

#### M-7.4 — Label immutability: existing accounts show no label field in settings (ledgers key on label); only new rows accept one.

**Steps:** open the settings modal — existing rows expose
id/type/enabled but no label input; a new row has one.

#### M-7.5 — Paper ⚙ settings: the reset + resize confirm flows still work from the popup; the adjust editor loads the current cash pools + holdings, edits apply (card value moves), a removed holding disappears, an added option row (symbol/expiry/strike/right/qty/avg) prices on the next poll.

**Steps:**

1. Paper card → ⚙ → reset → confirm → the card re-seeds
   from the live value.
2. ⚙ → adjust → the editor shows the current cash pools +
   holdings; change a qty, save → the card value moves on
   the next poll.
3. Remove a holding, save → gone from the positions table.
4. Add an option row (real symbol/expiry/strike), save →
   it prices within one `positions_refresh_seconds` poll.

#### M-7.6 — Add a real account in settings, restart the consumer, confirm the new paper account is seeded (automated in test_pipeline; verify the live flow once).

**Steps:**

1. Add a real account (M-7.1), restart.
2. A paper card for the new label appears, seeded from the
   live value; the pre-existing paper cards are untouched.

## 8. Build + release pipeline (CI)

**Automated** (`tests/test_build_zip.py`)

- Builds the real artifact into a temp dir and asserts: naming
  (`consumer-<8char>.zip`), `SHA256SUMS` matches the zip bytes,
  every client-install file present (`install_consumer.bat`,
  `start_consumer.bat`, `ws_login.py`, `apply_update.py`,
  `gen_cert.py`, `config/consumer.config.yaml`, `VERSION`, ...),
  no secrets/runtime files (`ws_tokens.env`, `*.db`,
  `__pycache__`), and the shipped template is the aligned one
  (blank accounts, `consumer:` section key, no token in it).

```bash
.venv/bin/python -m pytest tests/test_build_zip.py -q
```

**Manual**

#### M-8.1 — After each push: the `consumer-latest` tag moves to the new sha, the release has exactly one `consumer-<sha>.zip` + `SHA256SUMS`, and the zip passes the checksum.

**Steps:**

1. ```powershell
   git ls-remote origin refs/tags/consumer-latest
   # the tagged object points at the pushed head
   ```
2. GitHub → Releases → `consumer-latest`: exactly one zip +
   SHA256SUMS, both for the new sha.
3. Download both and verify:
   ```powershell
   Get-FileHash consumer-<sha>.zip -Algorithm SHA256
   # matches the hash line in SHA256SUMS
   ```

#### M-8.2 — Unzip a fresh artifact into a temp dir and run `install_consumer.bat` on a clean Windows box (or a new folder): venv created, `config/consumer.config.yaml` seeded, app starts against the info server.

**Steps:**

1. Unzip to a fresh folder; run
   `scripts\install_consumer.bat` (creates the venv,
   installs requirements, seeds
   `consumer\consumer.config.yaml` from the example).
2. Edit `consumer\consumer.config.yaml`: a local
   `auth_token`, the `feed:` section (info server URL +
   their consumer token), their
   `wealthsimple.accounts[]`.
3. `scripts\start_consumer.bat` — the dashboard comes up
   and pulls the feed from the info server.

## 9. Launchers (Windows)

**Manual**

#### M-9.1 — `start_consumer.bat` / `start_info.bat`: a missing per-role config prints the copy hint and pauses.

**Steps:**

1. ```powershell
   Rename-Item consumer\consumer.config.yaml consumer.config.yaml.bak
   scripts\start_consumer.bat
   # expect: consumer.config.yaml missing - copy ..\config\consumer.config.yaml here
   ```
2. Restore: `Rename-Item consumer.config.yaml.bak consumer.config.yaml`
   and start normally.

#### M-9.2 — Crash loop: kill the python process — the loop restarts in 5s, writes `pipeline_exit.txt`, and the next start surfaces "previous run: ...".

**Steps:**

1. Find the consumer's python (not the info server's):
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Where-Object CommandLine -match "consumer.config.yaml" |
     Select-Object ProcessId
   ```
2. `taskkill /F /PID <pid>` — the launcher window restarts
   it within 5s.
3. ```powershell
   Get-Content db\pipeline_exit_consumer.txt
   # -> consumer app exited with code <n>
   Get-Content logs\consumer.log -Tail 2
   # -> previous run: consumer app exited with code <n>
   ```
4. The Discord webhook shows `Consumer app restarting`
   with the previous exit as the reason (the info server
   posts `Info server restarting` the same way).
5. `taskkill /F /IM python.exe` kills BOTH roles at once —
   each role's launcher loop is independent, so both apps
   restart within 5s and both post their own restart
   notices. The `.bat` windows themselves are cmd.exe and
   are not affected.
6. Kill the launcher cmd for one role:
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
     Where-Object CommandLine -match "start_consumer.bat" |
     Select-Object ProcessId
   taskkill /F /PID <cmd-pid>
   ```
   Without `/T` the python child survives as an orphan: the
   app keeps running but is **unsupervised** — the next
   crash or update restart leaves it down until the `.bat`
   is started again. Restart the launcher to re-arm
   supervision.
7. Kill the whole tree (launcher + python):
   ```powershell
   taskkill /F /T /PID <cmd-pid>
   ```
   Nothing restarts (the supervisor is gone) — start the
   `.bat` again. This is the "closed the launcher window"
   scenario: closing a console window kills the attached
   python too.

#### M-9.3 — With a staged update pending, the launcher runs `apply_update.py` before relaunching and the app comes up on the new release (overlaps M-4.1).

**Steps:**

1. During M-4.1, if the app is stopped between staging and
   the self-restart, `consumer\.update_pending.json`
   exists.
2. `scripts\start_consumer.bat` — the launcher window
   prints `apply_update: applied <sha>` before starting
   python, and the banner shows the new release sha.

## 10. Full-stack regression

**Automated**

- `tests/scripts/e2e_test.py` — boots the real server end-to-end
  (config, db, feed, alert flow, auth guard, reader heartbeat).
- `tests/scripts/ui_test.py` — headless-Chrome dashboard smoke
  (31 checks incl. the settings save/revert lifecycle).
- `test_pipeline.py::test_clean_start_script` — the clean-start
  sweep is scoped to the role being cleaned: log files next to
  the db + that role's files in the shared `logs/` folder; a
  foreign (tmp) db never touches the real logs (this used to
  wipe the live `logs/` folder on every pytest run).

```bash
.venv/bin/python tests/scripts/e2e_test.py
.venv/bin/python tests/scripts/ui_test.py
.venv/bin/python -m pytest tests/test_pipeline.py -q -k "clean_start"
```

**Manual**

#### M-10.1 — Run both scripts after any updater/config/accounts change.

**Steps:**

1. ```powershell
   .venv\Scripts\python tests\scripts\e2e_test.py
   .venv\Scripts\python tests\scripts\ui_test.py
   ```
   Expect `RESULT: 64 passed, 0 failed` and
   `UI RESULT: 31 passed, 0 failed`.

#### M-10.2 — Notify-mode smoke on the live box: one alert flows reader → info → consumer → Discord embed, ledger row recorded, dashboard shows it.

**Steps:**

1. Inject a synthetic alert straight into the info server
   (the same route the reader posts to):
   ```powershell
   curl.exe -s -X POST http://127.0.0.1:8081/alert -H "X-Auth-Token: <info-token>" -H "Content-Type: application/json" -d "{\"text\":\"BOUGHT 0DTE SPX 6000c @ 1.50 small size\",\"author\":\"smoke-test\"}"
   # -> {"status":"ok", ...}
   ```
2. Within a second: the info dashboard's recent-alert feed
   shows it (parsed), and the consumer's dashboard shows
   the alert + a sizing/notify row (push or 1s pull).
3. The consumer's Discord webhook received the embed; the
   trade log records the row.
4. Dedupe: post the exact same text again — it is ignored
   (same message key inside the dedupe window).

## 11. Auth model

**Automated** (`tests/test_users.py`, `test_settings_update.py`,
`test_pipeline.py`, e2e phase D)

- The access token is mandatory: `main()` refuses to start
  without it on any host (`test_refuses_start_without_token`).
- First boot seeds the `admin` account from the token
  (`test_admin_seeded_from_access_token`); login is
  username+password only — no first-boot claim form, no bare
  token login (`test_tokenless_install_cannot_claim_or_bare_token_login`).
- Roles: admin/viewer, self password change requires the current
  password, viewer read-only (`test_users.py`).
- 5-fail/15-minute lockout; logout ends the session; the machine
  token (`X-Auth-Token`) grants owner-level API access
  (`test_auth_guard`, e2e auth phase).

```bash
.venv/bin/python -m pytest tests/test_users.py -q
.venv/bin/python -m pytest tests -q -k "auth_guard or refuses_start or login"
.venv/bin/python tests/scripts/e2e_test.py   # phase D = auth guard
```

**Manual**

#### M-11.1 — On the live box: log in as the seeded admin (the token as password), change the admin password from the users modal, restart, confirm the new password works and the old one does not.

**Steps:**

1. `http://127.0.0.1:8080/` → log in as `admin` /
   `<consumer-token>`.
2. Users panel → change the admin's password (current
   password required).
3. Logout → the old password (the token) is rejected; the
   new one logs in.
4. The machine token still opens the API (it is the
   reader's credential, independent of the password):
   ```powershell
   curl.exe -s -o NUL -w "%{http_code}" -H "X-Auth-Token: <consumer-token>" http://127.0.0.1:8080/api/summary
   # -> 200
   ```
5. Wrong-token negative:
   ```powershell
   curl.exe -s -o NUL -w "%{http_code}" -H "X-Auth-Token: wrong" http://127.0.0.1:8080/api/summary
   # -> 401
   ```

---

## Suggested order for a release checkpoint

1. Automated suite green (§1, §3, §4, §5, §7 automated parts).
2. `e2e_test.py` + `ui_test.py` (§10.1).
3. Push → verify CI release (§8.1) → consumer self-updates
   (§4 M-4.1) → info pull + reader restart (§5, §6).
4. Accounts round-trip on the live dashboard (§7 manual).
