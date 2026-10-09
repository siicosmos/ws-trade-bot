#!/usr/bin/env python3
import base64
import os
import subprocess
import sys
import tempfile
import time

import requests
import yaml

ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
# the venv layout differs per OS (Scripts\python.exe on windows)
if os.name == "nt":
    PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
else:
    PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"
_phase_seq = 0

PASS = 0
FAIL = 0
FAILURES = []


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


_current_token = ""


def write_config(path, mode, auth_token="", role="consumer", **trading):
    global _phase_seq
    _phase_seq += 1
    # the token is mandatory (it seeds the admin) - generate one
    # when the caller leaves it empty
    global _current_token
    if not auth_token:
        auth_token = f"e2e-token-{_phase_seq}"
    _current_token = auth_token
    cfg = {
        role: {
            "host": "127.0.0.1",
            "port": PORT + _phase_seq,
            "auth_token": auth_token,
        },
        "reader": {},
        "discord": {"trade_alert_webhook_url": ""},
        "auto_update": {"enabled": False},
        "quotes": {"provider": "ws"},
        "trading": {"mode": mode, **trading},
        "wealthsimple": {
            "accounts": [
                {
                    "account_id": "rrsp-test",
                    "label": "RRSP",
                    "paper_value": 50000,
                    "max_contracts_per_trade": 20,
                },
                {
                    "account_id": "pers-test",
                    "label": "Personal",
                    "paper_value": 2000,
                },
            ]
        },
        "parser": {},
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)


AUTH = requests.Session()


def start_pipeline(config_path, db_path):
    base = _current_base()
    err = tempfile.NamedTemporaryFile(
        "w+", prefix="e2e-pipeline-err-", delete=False
    )
    proc = subprocess.Popen(
        [PYTHON, os.path.join(ROOT, "run.py"), "-c", config_path, "--db", db_path],
        stdout=subprocess.DEVNULL,
        stderr=err,
        cwd=ROOT,
    )
    for _ in range(60):
        try:
            r = AUTH.get(f"{base}/health", timeout=1)
            if r.status_code == 200:
                # every later call in the phase rides the machine
                # token (the seeded admin's credential)
                AUTH.headers["X-Auth-Token"] = _current_token
                return proc, r.json()
        except requests.RequestException:
            pass
        time.sleep(0.25)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    err.flush()
    with open(err.name, encoding="utf-8") as f:
        tail = f.read()[-2000:]
    raise RuntimeError(
        f"pipeline did not start (stderr tail):\n{tail}"
    )


def _current_base():
    return f"http://127.0.0.1:{PORT + _phase_seq}"


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    base = _current_base()
    for _ in range(40):
        try:
            AUTH.get(f"{base}/health", timeout=1)
        except requests.RequestException:
            return
        time.sleep(0.25)
    raise RuntimeError("server still responding after stop")


def post_alert(text, headers=None, channel=""):
    payload = {"text": text, "author": "e2e"}
    if channel:
        payload["channel"] = channel
    r = AUTH.post(
        f"{_current_base()}/alert", json=payload, timeout=10,
        headers=headers or {},
    )
    if r.status_code != 200:
        return r.status_code, None
    return r.status_code, r.json()


def sizing_of(resp, label):
    for row in resp.get("sizing", []):
        if row.get("label") == label:
            return row
    return {}


def run_notify_phase():
    print("\n=== PHASE A: notify mode (alerts + sizing, no trading) ===")
    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "notify")
    proc, health = start_pipeline(cfg_path, db_path)
    try:
        summary = AUTH.get(f"{_current_base()}/api/summary", timeout=5).json()
        check("summary reports notify mode", summary.get("mode") == "notify")

        page = AUTH.get(f"{_current_base()}/", timeout=5)
        check("dashboard page serves", page.status_code == 200 and "WS Trade Bot" in page.text)

        summary = AUTH.get(f"{_current_base()}/api/summary", timeout=5).json()
        labels = {a["label"] for a in summary["accounts"]}
        check("summary lists both accounts", labels == {"RRSP", "Personal"})
        values = {a["label"]: a["value"] for a in summary["accounts"]}
        check("paper values used", values == {"RRSP": 50000.0, "Personal": 2000.0}, str(values))

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
        rrsp = sizing_of(resp, "RRSP")
        pers = sizing_of(resp, "Personal")
        check("small size -> notified", code == 200 and resp["status"] == "notified")

        code, resp = post_alert(
            "BOUGHT 0DTE SPY 758c @ 1.5 @everyone small size",
            channel="test-alerts",
        )
        sig = AUTH.get(f"{_current_base()}/api/signals", timeout=5).json()
        row = [s for s in sig if s.get("channel") == "test-alerts"]
        check("test channel recorded on signal", bool(row), str(sig[:2]))
        rrsp = sizing_of(resp, "RRSP")
        check("RRSP small sizing 2 contracts", rrsp.get("contracts") == 2, str(rrsp))
        check("RRSP tier cap warning", any("tier max" in w for w in rrsp.get("warnings", [])), str(rrsp))
        check("Personal small sizing 0 with warning", pers.get("contracts") == 0 and any("can't cover" in w for w in pers.get("warnings", [])), str(pers))

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size")
        rrsp = sizing_of(resp, "RRSP")
        check("medium sizing tier max 5", rrsp.get("contracts") == 5 and rrsp.get("risk_pct") == 5.0, str(rrsp))

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone lotto size")
        rrsp = sizing_of(resp, "RRSP")
        check("lotto sizing 1 contract", rrsp.get("contracts") == 1 and rrsp.get("risk_pct") == 0.5, str(rrsp))

        code, resp = post_alert("SOLD 1/4 0DTE SPY 759c @ 2.0 @everyone +30%")
        check("sell alert notified without sizing", resp["status"] == "notified" and resp.get("sizing") == [])
        check("sell scale parsed", resp["alert"]["scale"] == 0.25)

        code, resp = post_alert("executed 2.15 ^")
        check("fill confirmation ignored", resp["status"] == "ignored")

        upd = AUTH.get(f"{_current_base()}/api/update_status", timeout=5).json()
        check(
            "update status on portal",
            # the consumer follows the release channel: the head
            # is the VERSION commit (empty until the first check
            # seeds it), the branch reads "release"
            upd.get("status") == "active" and upd.get("branch") in (
                "release", "main",
            ),
            str(upd),
        )

        code, resp = post_alert("Typo on the last alert, it was 760c not 759c")
        check("correction forwarded without trading", code == 200 and resp["status"] == "correction", str(resp))

        signals = AUTH.get(f"{_current_base()}/api/signals", timeout=5).json()
        corr = [s for s in signals if s.get("correction")]
        check("signals flag correction rows", len(corr) == 1 and corr[0]["parsed"] == 0, str(signals[:2]))

        code, resp = post_alert("CORRECTION: SOLD 1/2 0DTE SPY 759c @ 2.0 @everyone")
        check("actionable correction notified with flag", resp["status"] == "notified" and resp.get("correction") is True, str(resp))

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
        check("duplicate message ignored", resp["status"] == "ignored")

        trades = AUTH.get(f"{_current_base()}/api/trades", timeout=5).json()
        check(
            "notify mode records a notified trade",
            bool(trades) and trades[0]["status"] == "notified"
            and trades[0]["mode"] == "notify",
        )
        positions = AUTH.get(f"{_current_base()}/api/positions", timeout=5).json()
        check("notify mode opens no positions", positions == [])
    finally:
        stop(proc)


def run_paper_cycle_phase():
    print("\n=== PHASE B: paper mode (full trade cycle + ledgers) ===")
    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "paper")
    proc, health = start_pipeline(cfg_path, db_path)
    try:
        summary = AUTH.get(f"{_current_base()}/api/summary", timeout=5).json()
        check("summary reports paper mode", summary.get("mode") == "paper")

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size")
        check("medium buy executed", resp["status"] == "executed" and resp["detail"], resp.get("status"))
        check("RRSP bought 5 tier-capped", "RRSP: 5x @ 1.5 (tier max 5" in resp["detail"], resp["detail"])
        check("Personal skipped for budget", "Personal: 0 (budget" in resp["detail"], resp["detail"])

        positions = AUTH.get(f"{_current_base()}/api/positions", timeout=5).json()
        by_acct = {p["account"]: p for p in positions}
        check("ledger shows RRSP 5 contracts", by_acct.get("RRSP", {}).get("qty") == 5, str(positions))
        check("Personal has no position", "Personal" not in by_acct, str(positions))

        code, resp = post_alert("SOLD 1/4 0DTE SPY 759c @ 1.95 @everyone +30%")
        check("scale-out sells 1 of 5", resp["status"] == "executed" and "RRSP: 1/5x @ 1.95" in resp["detail"], resp.get("detail"))

        code, resp = post_alert("ALL OUT 0DTE SPY 759c @ 1.5 @everyone out rest BE")
        check("all out closes 4", resp["status"] == "executed" and "RRSP: 4/4x @ 1.5" in resp["detail"], resp.get("detail"))

        positions = AUTH.get(f"{_current_base()}/api/positions", timeout=5).json()
        check("position closed", positions == [], str(positions))

        summary = AUTH.get(f"{_current_base()}/api/summary", timeout=5).json()
        rrsp_value = [a for a in summary["accounts"] if a["label"] == "RRSP"][0]["value"]
        expected = 50000 - 5 * 150 + 1 * 195 + 4 * 150
        check("paper equity tracks realized P/L", rrsp_value == expected, f"{rrsp_value} != {expected}")

        trades = AUTH.get(f"{_current_base()}/api/trades", timeout=5).json()
        check("trade log has 3 executed rows", len([t for t in trades if t["status"] == "executed"]) == 3, str(len(trades)))
    finally:
        stop(proc)


def run_risk_gate_phase():
    print("\n=== PHASE C: risk gates (daily cap, cooldown, open risk) ===")

    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "paper", max_trades_per_day=1)
    proc, _ = start_pipeline(cfg_path, db_path)
    try:
        post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
        code, resp = post_alert("BOUGHT 0DTE SPY 760c @ 1.5 @everyone small size")
        check("daily cap blocks second buy", resp["status"] == "blocked" and "daily trade limit" in resp["reason"], str(resp))
    finally:
        stop(proc)

    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "paper", cooldown_seconds=3600)
    proc, _ = start_pipeline(cfg_path, db_path)
    try:
        post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
        code, resp = post_alert("BOUGHT 0DTE SPY 760c @ 1.5 @everyone small size")
        check("cooldown blocks rapid second buy", resp["status"] == "blocked" and "cooldown" in resp["reason"], str(resp))
    finally:
        stop(proc)

    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "paper", max_open_risk_pct=1, cooldown_seconds=0, max_trades_per_day=10)
    proc, _ = start_pipeline(cfg_path, db_path)
    try:
        post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size")
        code, resp = post_alert("BOUGHT 0DTE SPY 760c @ 1.5 @everyone medium size")
        check("open risk cap blocks new buys", resp["status"] == "skipped" and "open risk" in resp["detail"], str(resp))
        trades = AUTH.get(f"{_current_base()}/api/trades", timeout=5).json()
        check(
            "first buy still executed",
            any(t["status"] == "executed" for t in trades),
            str(trades),
        )
    finally:
        stop(proc)


def run_auth_phase():
    print("\n=== PHASE D: auth guard ===")
    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "notify", auth_token="e2e-secret")
    proc, _ = start_pipeline(cfg_path, db_path)
    # this phase tests raw access - drop the machine-token header
    # that start_pipeline armed
    AUTH.headers.pop("X-Auth-Token", None)
    try:
        resp = requests.get(f"{_current_base()}/", timeout=5, allow_redirects=False)
        check(
            "dashboard redirects to login without session",
            resp.status_code == 302 and resp.headers["Location"].endswith("/login"),
        )
        browser = requests.Session()
        login_page = browser.get(f"{_current_base()}/login", timeout=5, verify=False)
        check("login page served", login_page.status_code == 200 and "username" in login_page.text)
        bad_login = browser.post(
            f"{_current_base()}/login",
            data={"username": "admin", "password": "wrong"},
            timeout=5, verify=False,
        )
        check("wrong password rejected",
              "wrong username or password" in bad_login.text)
        browser.post(
            f"{_current_base()}/login",
            data={"username": "admin", "password": "e2e-secret"},
            timeout=5, verify=False, allow_redirects=False,
        )
        check(
            "session cookie grants dashboard access",
            browser.get(f"{_current_base()}/", timeout=5, verify=False).status_code == 200,
        )
        browser.get(f"{_current_base()}/logout", timeout=5, verify=False)
        check(
            "logout ends the session",
            requests.get(
                f"{_current_base()}/", timeout=5, verify=False, allow_redirects=False,
            ).status_code == 302,
        )
        check("api blocked without token", requests.get(f"{_current_base()}/api/summary", timeout=5).status_code == 401)
        check("health open without token", requests.get(f"{_current_base()}/health", timeout=5).status_code == 200)
        check(
            "token header grants access",
            requests.get(f"{_current_base()}/api/summary", headers={"X-Auth-Token": "e2e-secret"}, timeout=5).status_code == 200,
        )
        basic = base64.b64encode(b"user:e2e-secret").decode()
        check(
            "basic auth no longer grants access (logout works)",
            requests.get(f"{_current_base()}/api/summary", headers={"Authorization": f"Basic {basic}"}, timeout=5).status_code == 401,
        )
        check(
            "wrong token rejected",
            requests.get(f"{_current_base()}/api/summary", headers={"X-Auth-Token": "wrong"}, timeout=5).status_code == 401,
        )
        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
        check("alert blocked without token", code == 401)
        code, resp = post_alert(
            "BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size",
            headers={"X-Auth-Token": "e2e-secret"},
        )
        check("alert accepted with token", code == 200 and resp["status"] == "notified")
    finally:
        stop(proc)


def run_settings_phase():
    print("\n=== PHASE E: live settings via portal ===")
    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "notify")
    proc, health = start_pipeline(cfg_path, db_path)
    try:
        base = _current_base()
        settings = AUTH.get(f"{base}/api/settings", timeout=5).json()
        check("settings readable", settings["trading"]["stop_loss_pct"] == 25)

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size")
        rrsp = sizing_of(resp, "RRSP")
        check("baseline medium tier caps at 5", rrsp.get("contracts") == 5 and rrsp.get("risk_pct") == 5.0, str(rrsp))

        r = AUTH.post(
            f"{base}/api/settings",
            json={"trading": {"size_tiers": {
                "medium": {"risk_pct_max": 3, "contracts_min": 1,
                           "contracts_max": 3}
            }}},
            timeout=5,
        )
        check("settings update accepted", r.status_code == 200, r.text[:200])

        code, resp = post_alert("BOUGHT 0DTE SPY 760c @ 1.5 @everyone medium size")
        rrsp = sizing_of(resp, "RRSP")
        check(
            "new tier takes effect immediately",
            rrsp.get("contracts") == 3 and rrsp.get("risk_pct") == 3.0,
            str(rrsp),
        )

        with open(cfg_path, encoding="utf-8") as f:
            content = f.read()
        check("settings persisted to config.yaml", "risk_pct_max: 3" in content)

        r = AUTH.post(
            f"{base}/api/settings",
            json={"trading": {"stop_loss_pct": 999}},
            timeout=5,
        )
        check("invalid settings rejected", r.status_code == 400)

        update = AUTH.get(f"{base}/api/update_status", timeout=5).json()
        check("update status endpoint", "status" in update, str(update))

        code, resp = post_alert("BOUGHT 0DTE SPY 761c @ 1.5 @everyone big size")
        rrsp = sizing_of(resp, "RRSP")
        check("account cap 20 allows big tier", rrsp.get("contracts") == 10, str(rrsp))

        r = AUTH.post(
            f"{base}/api/settings",
            json={"accounts": [
                {"label": "RRSP", "max_contracts_per_trade": 2},
            ]},
            timeout=5,
        )
        check("portal sets account cap", r.status_code == 200, r.text[:200])

        code, resp = post_alert("BOUGHT 0DTE SPY 762c @ 1.5 @everyone big size")
        rrsp = sizing_of(resp, "RRSP")
        check(
            "account cap change applies immediately",
            rrsp.get("contracts") == 2 and "capped at account max of 2" in str(rrsp.get("warnings")),
            str(rrsp),
        )
    finally:
        stop(proc)


def run_reader_status_phase():
    print("\n=== PHASE F: reader heartbeat on the info server ===")
    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "notify", role="info", auth_token="info-token")
    # the reader heartbeats the info server (its alert source);
    # reader knobs are config-file managed there
    import yaml as _yaml
    with open(cfg_path, encoding="utf-8") as f:
        cfg_raw = _yaml.safe_load(f)
    cfg_raw["reader"]["channels"] = ["player-alerts"]
    cfg_raw["consumers"] = []
    with open(cfg_path, "w", encoding="utf-8") as f:
        _yaml.safe_dump(cfg_raw, f)
    proc, health = start_pipeline(cfg_path, db_path)
    try:
        base = _current_base()
        hdr = {"X-Auth-Token": "info-token"}
        r = AUTH.post(
            f"{base}/api/reader_status",
            json={"channel": "test-channel", "ok": True},
            headers=hdr, timeout=5,
        ).json()
        # the heartbeat no longer pushes reader settings (they
        # live in config/reader.config.yaml and would be
        # overridden on every poll) - it acknowledges with {}
        check("reader heartbeat acknowledges", r == {}, str(r))

        status = AUTH.get(
            f"{base}/api/reader_status", headers=hdr, timeout=5,
        ).json()
        check("reader status shows the channel",
              status["channel"] == "test-channel", str(status))
        check("reader status shows desired marker",
              # desired comes from config/reader.config.yaml on
              # this machine - only assert the field exists
              "desired" in status, str(status))
        check("reader status shows heartbeat age",
              status["age_seconds"] is not None, str(status))

        # the info app has no trading routes
        r = AUTH.get(f"{base}/api/positions", timeout=5)
        check("info app has no trading routes", r.status_code == 404, str(r.status_code))

        # the feed answers a token-authed head-only poll
        r = AUTH.get(
            f"{base}/api/feed",
            headers={"X-Auth-Token": "nope"}, timeout=5,
        )
        check("feed rejects a bad consumer token", r.status_code == 401, str(r.status_code))
    finally:
        stop(proc)


def run_ui_smoke_phase():
    """Headless-chrome dashboard smoke test (skips without a
    browser on PATH)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ui_test",
        os.path.join(ROOT, "tests", "scripts", "ui_test.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not mod.find_chrome():
        print("  SKIP  ui-smoke (no chrome/chromium on PATH)")
        return
    print("\n--- UI smoke phase ---")
    rc = mod.main()
    check("ui-smoke-suite", rc == 0)


def main():
    run_notify_phase()
    run_paper_cycle_phase()
    run_risk_gate_phase()
    run_auth_phase()
    run_settings_phase()
    run_reader_status_phase()
    run_ui_smoke_phase()

    print(f"\n{'=' * 50}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
