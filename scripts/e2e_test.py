#!/usr/bin/env python3
import base64
import os
import subprocess
import sys
import tempfile
import time

import requests
import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
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


def write_config(path, mode, auth_token="", **trading):
    global _phase_seq
    _phase_seq += 1
    cfg = {
        "pipeline": {
            "host": "127.0.0.1",
            "port": PORT + _phase_seq,
            "auth_token": auth_token,
        },
        "reader": {},
        "discord": {"webhook_url": ""},
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
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f)


def start_pipeline(config_path, db_path):
    base = _current_base()
    proc = subprocess.Popen(
        [PYTHON, os.path.join(ROOT, "run.py"), "-c", config_path, "--db", db_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=ROOT,
    )
    for _ in range(60):
        try:
            r = requests.get(f"{base}/health", timeout=1)
            if r.status_code == 200:
                return proc, r.json()
        except requests.RequestException:
            pass
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError("pipeline did not start")


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
            requests.get(f"{base}/health", timeout=1)
        except requests.RequestException:
            return
        time.sleep(0.25)
    raise RuntimeError("server still responding after stop")


def post_alert(text, headers=None):
    r = requests.post(
        f"{_current_base()}/alert", json={"text": text, "author": "e2e"}, timeout=10,
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
        check("health reports notify mode", health.get("mode") == "notify")

        page = requests.get(f"{_current_base()}/", timeout=5)
        check("dashboard page serves", page.status_code == 200 and "WS Trade Bot" in page.text)

        summary = requests.get(f"{_current_base()}/api/summary", timeout=5).json()
        labels = {a["label"] for a in summary["accounts"]}
        check("summary lists both accounts", labels == {"RRSP", "Personal"})
        values = {a["label"]: a["value"] for a in summary["accounts"]}
        check("paper values used", values == {"RRSP": 50000.0, "Personal": 2000.0}, str(values))

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
        rrsp = sizing_of(resp, "RRSP")
        pers = sizing_of(resp, "Personal")
        check("small size -> notified", code == 200 and resp["status"] == "notified")
        check("RRSP small sizing 2 contracts", rrsp.get("contracts") == 2, str(rrsp))
        check("RRSP tier cap warning", any("tier max" in w for w in rrsp.get("warnings", [])), str(rrsp))
        check("Personal small sizing 0 with warning", pers.get("contracts") == 0 and any("per-contract cost" in w for w in pers.get("warnings", [])), str(pers))

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

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone small size")
        check("duplicate message ignored", resp["status"] == "ignored")

        trades = requests.get(f"{_current_base()}/api/trades", timeout=5).json()
        check("notify mode records no trades", trades == [])
        positions = requests.get(f"{_current_base()}/api/positions", timeout=5).json()
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
        check("health reports paper mode", health.get("mode") == "paper")

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size")
        check("medium buy executed", resp["status"] == "executed" and resp["detail"], resp.get("status"))
        check("RRSP bought 5 tier-capped", "RRSP: 5x @ 1.5 (tier max 5" in resp["detail"], resp["detail"])
        check("Personal skipped for budget", "Personal: 0 (budget" in resp["detail"], resp["detail"])

        positions = requests.get(f"{_current_base()}/api/positions", timeout=5).json()
        by_acct = {p["account"]: p for p in positions}
        check("ledger shows RRSP 5 contracts", by_acct.get("RRSP", {}).get("qty") == 5, str(positions))
        check("Personal has no position", "Personal" not in by_acct, str(positions))

        code, resp = post_alert("SOLD 1/4 0DTE SPY 759c @ 1.95 @everyone +30%")
        check("scale-out sells 1 of 5", resp["status"] == "executed" and "RRSP: 1/5x @ 1.95" in resp["detail"], resp.get("detail"))

        code, resp = post_alert("ALL OUT 0DTE SPY 759c @ 1.5 @everyone out rest BE")
        check("all out closes 4", resp["status"] == "executed" and "RRSP: 4/4x @ 1.5" in resp["detail"], resp.get("detail"))

        positions = requests.get(f"{_current_base()}/api/positions", timeout=5).json()
        check("position closed", positions == [], str(positions))

        summary = requests.get(f"{_current_base()}/api/summary", timeout=5).json()
        rrsp_value = [a for a in summary["accounts"] if a["label"] == "RRSP"][0]["value"]
        expected = 50000 - 5 * 150 + 1 * 195 + 4 * 150
        check("paper equity tracks realized P/L", rrsp_value == expected, f"{rrsp_value} != {expected}")

        trades = requests.get(f"{_current_base()}/api/trades", timeout=5).json()
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
        trades = requests.get(f"{_current_base()}/api/trades", timeout=5).json()
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
    try:
        check("dashboard blocked without token", requests.get(f"{_current_base()}/", timeout=5).status_code == 401)
        check("api blocked without token", requests.get(f"{_current_base()}/api/summary", timeout=5).status_code == 401)
        check("health open without token", requests.get(f"{_current_base()}/health", timeout=5).status_code == 200)
        check(
            "token header grants access",
            requests.get(f"{_current_base()}/api/summary", headers={"X-Auth-Token": "e2e-secret"}, timeout=5).status_code == 200,
        )
        basic = base64.b64encode(b"user:e2e-secret").decode()
        check(
            "basic auth grants access",
            requests.get(f"{_current_base()}/api/summary", headers={"Authorization": f"Basic {basic}"}, timeout=5).status_code == 200,
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
        settings = requests.get(f"{base}/api/settings", timeout=5).json()
        check("settings readable", settings["trading"]["stop_loss_pct"] == 25)

        code, resp = post_alert("BOUGHT 0DTE SPY 759c @ 1.5 @everyone medium size")
        rrsp = sizing_of(resp, "RRSP")
        check("baseline medium tier caps at 5", rrsp.get("contracts") == 5 and rrsp.get("risk_pct") == 5.0, str(rrsp))

        r = requests.post(
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

        with open(cfg_path) as f:
            content = f.read()
        check("settings persisted to config.yaml", "risk_pct_max: 3" in content)

        r = requests.post(
            f"{base}/api/settings",
            json={"trading": {"stop_loss_pct": 999}},
            timeout=5,
        )
        check("invalid settings rejected", r.status_code == 400)

        update = requests.get(f"{base}/api/update_status", timeout=5).json()
        check("update status endpoint", "status" in update, str(update))

        code, resp = post_alert("BOUGHT 0DTE SPY 761c @ 1.5 @everyone big size")
        rrsp = sizing_of(resp, "RRSP")
        check("account cap 20 allows big tier", rrsp.get("contracts") == 10, str(rrsp))

        r = requests.post(
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
            rrsp.get("contracts") == 2 and "capped at account max 2" in str(rrsp.get("warnings")),
            str(rrsp),
        )
    finally:
        stop(proc)


def run_reader_status_phase():
    print("\n=== PHASE F: reader channel control via portal ===")
    cfg_path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    db_path = os.path.join(tempfile.mkdtemp(), "trades.db")
    write_config(cfg_path, "notify")
    proc, health = start_pipeline(cfg_path, db_path)
    try:
        base = _current_base()
        r = requests.post(
            f"{base}/api/reader_status",
            json={"channel": "test-channel", "ok": True}, timeout=5,
        ).json()
        check("reader heartbeat returns config", r["channel_marker"] == "", str(r))

        r = requests.post(
            f"{base}/api/settings",
            json={"reader": {"channel_marker": "player-alerts",
                             "poll_interval": 0.75}},
            timeout=5,
        )
        check("portal sets channel marker", r.status_code == 200, r.text[:200])

        r = requests.post(
            f"{base}/api/reader_status",
            json={"channel": "test-channel", "ok": True}, timeout=5,
        ).json()
        check("reader receives new marker", r["channel_marker"] == "player-alerts", str(r))
        check("reader receives new poll interval", r["poll_interval"] == 0.75, str(r))

        summary = requests.get(f"{base}/api/summary", timeout=5).json()
        check("summary shows reader watching", summary["reader"]["channel"] == "test-channel")
        check("summary shows desired channel", summary["reader"]["desired"] == "player-alerts")
        check("summary shows heartbeat age", summary["reader"]["age_seconds"] is not None)

        r = requests.post(
            f"{base}/api/settings",
            json={"reader": {"channel_marker": ""}},
            timeout=5,
        )
        check("marker can be cleared for follow mode", r.status_code == 200)
        r = requests.post(
            f"{base}/api/reader_status",
            json={"channel": "whatever", "ok": True}, timeout=5,
        ).json()
        check("cleared marker reaches reader", r["channel_marker"] == "")
    finally:
        stop(proc)


def main():
    run_notify_phase()
    run_paper_cycle_phase()
    run_risk_gate_phase()
    run_auth_phase()
    run_settings_phase()
    run_reader_status_phase()

    print(f"\n{'=' * 50}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
