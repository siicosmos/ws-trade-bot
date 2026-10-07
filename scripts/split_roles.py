#!/usr/bin/env python3
"""Split a monolith install into the info/consumer roles.

Run once on the machine that hosts the reader (the owner's
Windows box). Produces, next to the existing config.yaml and
trades.db:

  config_info.yaml      role: info   - reader ingest + alert feed
  trades-info.db        copy of trades.db (signals drive the feed)
  config_consumer.yaml  role: consumer - the trading app (yours)
  trades-consumer.db    copy of trades.db (full history carries over)

The reader config keeps pointing at localhost:8080, which stays
the info server; the consumer app moves to port 8081 and is
registered as a push consumer. Existing start_pipeline.bat is
replaced by start_info.bat + start_consumer.bat.

Usage:  python scripts/split_roles.py [-c config.yaml] [--db trades.db]
"""

import argparse
import os
import secrets
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--db", default="trades.db")
    args = ap.parse_args()

    root = os.path.dirname(os.path.abspath(args.config))
    with open(args.config) as f:
        cfg = yaml.safe_load(f) or {}

    if str((cfg.get("pipeline") or {}).get("role", "consumer")) == "info":
        print("config already has role: info - nothing to do")
        return

    consumer_token = secrets.token_urlsafe(24)

    # ---- info server: keeps port 8080 (the reader target) and
    # the original auth_token (the reader's credential)
    info = yaml.safe_load(yaml.safe_dump(cfg)) or {}
    info.setdefault("pipeline", {})["role"] = "info"
    info["consumers"] = [
        {
            "label": "owner",
            "token": consumer_token,
            # same box: push straight into the consumer app
            "push_url": "http://127.0.0.1:8081/alert",
        }
    ]
    info["reader"] = cfg.get("reader") or {}

    # ---- consumer app: port 8081, own token, feed at localhost
    consumer = yaml.safe_load(yaml.safe_dump(cfg)) or {}
    consumer.setdefault("pipeline", {})["role"] = "consumer"
    consumer["pipeline"]["port"] = 8081
    consumer["pipeline"]["auth_token"] = consumer_token
    consumer["feed"] = {
        "url": "http://127.0.0.1:8080",
        "token": consumer_token,
        "poll_seconds": 1.0,
    }
    # the reader belongs to the info server - drop it here so a
    # stray reader start against the consumer fails loudly
    consumer.pop("reader", None)

    for name, data in (
        ("config_info.yaml", info),
        ("config_consumer.yaml", consumer),
    ):
        path = os.path.join(root, name)
        if os.path.exists(path):
            print(f"refusing to overwrite {path} - move it aside first")
            sys.exit(1)
        with open(path, "w") as f:
            yaml.safe_dump(
                data, f, default_flow_style=False, sort_keys=False,
                allow_unicode=True, width=4096,
            )
        print("wrote " + path)

    for name in ("trades-info.db", "trades-consumer.db"):
        path = os.path.join(root, name)
        if os.path.exists(path):
            print(f"keeping existing {path}")
            continue
        shutil.copy2(args.db, path)
        print(f"copied {args.db} -> {path}")

    print(
        """
next steps:
  1. start the info server:   scripts\\start_info.bat
  2. start your consumer app: scripts\\start_consumer.bat
     (dashboard on http://127.0.0.1:8081 - expose it via
      Tailscale if you want it on your phone)
  3. the reader keeps running as-is (it still posts to :8080)
  4. other users: install this repo on their machine, copy
     config.example.yaml, set role: consumer + feed url/token,
     and add a consumer entry with their token here
  5. retire start_pipeline.bat once both roles run
"""
    )


if __name__ == "__main__":
    main()
