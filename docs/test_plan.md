# Test plan

Covers the release-install client, the role-split updater, the
per-role file naming, and the account model. Each area lists the
automated coverage that already exists (pytest), the gaps worth
automating, and the manual procedures that need a real machine,
real network, or real Discord/Wealthsimple.

Run the automated suite with:

```bash
.venv/bin/python -m pytest tests -q --ignore=tests/scripts
```

Known env-dependent failures (not regressions):
`test_ws_http_shim_injects_timeout` (moomoo module missing),
`test_heartbeat_fires_while_channel_quiet` /
`test_unsent_messages_are_retried` (reader timing).

---

## 1. Config system

**Automated** (`tests/test_config_example.py`)

- `consumer:` / `info:` section keys load; the section key IS the
  role (no `role:` field needed).
- Legacy `pipeline:` + `role:` still loads; section key wins over
  a stale `role:` field.
- Templates parse, load through `load_config`, carry the right
  role/port defaults, ship no secrets.
- Reader template is flat-key.
- Account `type` normalized (`MARGIN` → `margin`, unknown → `""`).

**Manual**

- [ ] M-1.1 Start each role once from its renamed config
      (`consumer/consumer.config.yaml`, `info/info.config.yaml`)
      and confirm the dashboard + role dispatch work.
- [ ] M-1.2 A config with BOTH `pipeline:` and `consumer:` loads
      with the `consumer:` section winning (no crash, no dupes).

## 2. Per-role file names + migration

**Automated**

- `test_config_example.py::test_run_db_migration` — trades.db →
  `<role>.trades.db` rename incl. `-wal`/`-shm`, idempotent,
  untouched when the old file is missing.
- `test_release_updater.py` apply test — state files survive the
  release swap (new + legacy names).

**Manual**

- [ ] M-2.1 On the live box: stop both apps, confirm
      `consumer/consumer.trades.db` + `info/info.trades.db` exist
      (renamed 2026-10-08), restart via the `.bat`s, and confirm
      the dashboards still show full history (no fresh empty db).
- [ ] M-2.2 Delete `VERSION`-less edge: rename the consumer db
      back to `trades.db`, start the consumer, confirm run.py
      migrates it and prints `migrated ...`.

## 3. Updater routing (role-based)

**Automated** (`tests/test_release_updater.py`)

- consumer role → `ReleaseUpdater` even inside a git checkout;
  info role → `AutoUpdater`.
- Release updater seeds `VERSION` from the git checkout (head +
  origin slug) on the first check, then compares releases.
- `update_status_payload` works for both updater types.

**Manual**

- [ ] M-3.1 Consumer (git checkout, live box): start it, watch
      `consumer.log` for
      `release channel seeded from the git checkout (<sha>)`,
      then `up to date` — and NO `git fetch` / dirty-tree lines.
- [ ] M-3.2 Info server: confirm it still logs the git-poll
      cycle and pulls on a new push to main.

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

**Gap worth automating**

- G-4.1 `apply_update` failure path: inject a copytree failure
  mid-swap and assert the state files are restored from
  `.update_backup` (the restore branch is currently untested).
- G-4.2 Release asset with a missing `core/` dir → "staged build
  incomplete - aborted" (the guard exists; no test hits it).

**Manual (real network + real release)**

- [ ] M-4.1 End-to-end self-update: push a trivial commit to
      main → CI publishes `consumer-latest` → within
      `interval_seconds` the consumer logs
      `staged <sha> - restarting to apply...`, exits 77, the
      launcher applies the swap, and the banner shows the new
      release sha. Confirm config/db/webhooks survived.
- [ ] M-4.2 Checksum tamper: hand-edit a byte in a staged zip
      copy and confirm the mismatch abort (or verify the CI
      SHA256SUMS matches the published zip with `sha256sum -c`).
- [ ] M-4.3 Private-repo token: with `github_token` unset, the
      poll fails gracefully (`release check failed: 401/404`);
      with a read-only token set it succeeds.
- [ ] M-4.4 Poll-during-publish race: while CI recreates the
      release, a poll may see a 404 — confirm it logs and
      retries next cycle without restarting.

## 5. Git updater (info server) + restart scoping

**Automated** (`tests/test_updater.py`, `test_pipeline.py`)

- Fetch + ff-only pull, restart only when role code changed,
  untracked files never block, dirty tracked files block,
  ignored runtime files are healed, stale `index.lock` cleared,
  update record roundtrip, interval change applies mid-cycle.
- Webhook titles are role-branded: `Info server restarting` /
  `Consumer app restarting` / `... updated (no restart)`.

**Manual**

- [ ] M-5.1 Push a docs-only commit: info server pulls, posts
      "Info server updated (no restart)", keeps running.
- [ ] M-5.2 Push an `info/*` commit: pulls, posts
      "Info server restarting", restarts, banner shows the new
      commit.
- [ ] M-5.3 Discord: confirm the embed titles match the role
      (no more "Pipeline restarting") for info + consumer.

## 6. Reader (rides the info pull)

**Automated**: reader logic is Windows/UIA-bound — covered only
by stubs (`test_reader.py` timing tests).

**Manual (Windows box)**

- [ ] M-6.1 Legacy rename: with `reader/config.yaml` present and
      `reader.config.yaml` absent, start the reader — confirm it
      renames, logs it, and loads the old settings/webhooks.
- [ ] M-6.2 Push a `reader/*` commit → info server pulls → the
      reader logs `repo updated on disk - restarting reader`,
      posts "Reader restarting", and comes back watching Discord.
- [ ] M-6.3 Push a docs-only commit → reader logs
      `repo updated (no reader changes) - staying up`.
- [ ] M-6.4 Edit `reader/reader.config.yaml` while running →
      reader restarts to apply it.

## 7. Accounts (add/remove, margin model)

**Automated** (`tests/test_settings_update.py`,
`test_config_example.py`)

- Blank config → add two accounts (margin + non_margin) →
  persisted with types; remove one; flip type back to auto.
- Validation: bad type reported, missing label rejected,
  unknown label = add, known label = update.
- `account_is_non_margin`: explicit type wins over API type and
  label keywords; API type next; label keyword last.
- Consumer example ships `accounts: []`.

**Manual (dashboard + real WS)**

- [ ] M-7.1 Settings modal: add an account row (label, id,
      type=non_margin), save, restart, confirm the account card
      appears and sizing uses it.
- [ ] M-7.2 Remove an account, save, restart — card gone,
      ledger rows still in the db.
- [ ] M-7.3 Margin behavior: the margin account shows a real
      margin breakdown; the RRSP (non_margin) suppresses it —
      also confirm an explicit `type` overrides what the WS API
      reports (set RRSP to `margin` temporarily and watch the
      card flip, then set it back).
- [ ] M-7.4 Label immutability: existing accounts show no label
      field in settings (ledgers key on label); only new rows
      accept one.

## 8. Build + release pipeline (CI)

**Automated**: `build_consumer_zip.py` output shape is asserted
only indirectly; the workflow itself is not tested.

**Gap worth automating**

- G-8.1 A test that runs `build_consumer_zip.py` into tmp and
  asserts: no `config.yaml`/`ws_tokens.env`/`trades.db*`, has
  `config/consumer.config.yaml` + `VERSION` + `SHA256SUMS`
  matching the zip hash, `.bat` files present, `__pycache__`
  excluded.

**Manual**

- [ ] M-8.1 After each push: the `consumer-latest` tag moves to
      the new sha, the release has exactly one
      `consumer-<sha>.zip` + `SHA256SUMS`, and the zip passes
      `sha256sum -c`.
- [ ] M-8.2 Unzip a fresh artifact into a temp dir and run
      `install_consumer.bat` on a clean Windows box (or a new
      folder): venv created, `consumer/consumer.config.yaml`
      seeded, app starts against the info server.

## 9. Launchers (Windows)

**Manual**

- [ ] M-9.1 `start_consumer.bat` / `start_info.bat`: old
      `config.yaml` auto-renames on first run; missing config
      prints the new copy hint and pauses.
- [ ] M-9.2 Crash loop: kill the python process — the loop
      restarts in 5s, writes `pipeline_exit.txt`, and the next
      start surfaces "previous run: ...".
- [ ] M-9.3 With a staged update pending, the launcher runs
      `apply_update.py` before relaunching and the app comes up
      on the new release (overlaps M-4.1).

## 10. Full-stack regression

**Automated**

- `tests/scripts/e2e_test.py` — boots the real server end-to-end
  (config, db, feed, alert flow).
- `tests/scripts/ui_test.py` — headless-Chrome dashboard smoke.

**Manual**

- [ ] M-10.1 Run both scripts after any updater/config/accounts
      change.
- [ ] M-10.2 Notify-mode smoke on the live box: one alert flows
      reader → info → consumer → Discord embed, ledger row
      recorded, dashboard shows it.

---

## Suggested order for a release checkpoint

1. Automated suite green (§1, §3, §4, §5, §7 automated parts).
2. `e2e_test.py` + `ui_test.py` (§10.1).
3. Push → verify CI release (§8.1) → consumer self-updates
   (§4 M-4.1) → info pull + reader restart (§5, §6).
4. Accounts round-trip on the live dashboard (§7 manual).
