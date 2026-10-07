#!/usr/bin/env python3
"""Split a monolith install into the info/consumer roles.

Run once on the machine that hosts the reader (the owner's
Windows box). Each role gets its own folder under the repo:

  info/
    config.yaml      role: info - reader ingest + alert feed
    trades.db        copy of trades.db (signals drive the feed)
                     launcher: scripts\start_info.bat (own logs/exit file)
  consumer/
    config.yaml      role: consumer - the trading app (yours)
    trades.db        copy of trades.db (full history carries over)
                     launcher: scripts\start_consumer.bat

The reader config keeps pointing at localhost:8080, which stays
the info server; the consumer app moves to port 8081 and is
registered as a push consumer. ws_tokens.env stays at the repo
root (shared by both roles on this machine).

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

    # ---- already split into folders? nothing to do
    if (
        os.path.exists(os.path.join(root, "info", "config.yaml"))
        and os.path.exists(
            os.path.join(root, "consumer", "config.yaml")
        )
    ):
        print("info/ and consumer/ configs already exist - nothing to do")
        return

    # ---- flat layout from an earlier run of this script? move
    # the files into the folders (tokens and history preserved)
    flat_info = os.path.join(root, "config_info.yaml")
    flat_consumer = os.path.join(root, "config_consumer.yaml")
    if os.path.exists(flat_info) and os.path.exists(flat_consumer):
        for folder, cfg_name, db_names in (
            ("info", "config_info.yaml",
             ("trades-info.db",)),
            ("consumer", "config_consumer.yaml",
             ("trades-consumer.db",)),
        ):
            target = os.path.join(root, folder)
            os.makedirs(target, exist_ok=True)
            src = os.path.join(root, cfg_name)
            dst = os.path.join(target, "config.yaml")
            if os.path.exists(dst):
                print(f"refusing to overwrite {dst}")
                sys.exit(1)
            shutil.move(src, dst)
            print(f"moved {src} -> {dst}")
            for db_name in db_names:
                db_src = os.path.join(root, db_name)
                if os.path.exists(db_src):
                    db_dst = os.path.join(target, "trades.db")
                    if os.path.exists(db_dst):
                        print(f"keeping existing {db_dst}")
                        continue
                    shutil.move(db_src, db_dst)
                    print(f"moved {db_src} -> {db_dst}")
        print(
            "\nnext steps:\n"
            "  1. start the info server:   scripts\\start_info.bat\n"
            "  2. start your consumer app: scripts\\start_consumer.bat\n"
            "  3. the reader keeps running as-is (it still posts"
            " to :8080)\n"
        )
        return

    # ---- monolith: split config.yaml + trades.db into the roles
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

    for folder, data in (("info", info), ("consumer", consumer)):
        target = os.path.join(root, folder)
        os.makedirs(target, exist_ok=True)
        path = os.path.join(target, "config.yaml")
        if os.path.exists(path):
            print(f"refusing to overwrite {path} - move it aside first")
            sys.exit(1)
        with open(path, "w") as f:
            yaml.safe_dump(
                data, f, default_flow_style=False, sort_keys=False,
                allow_unicode=True, width=4096,
            )
        print("wrote " + path)

    for folder in ("info", "consumer"):
        path = os.path.join(root, folder, "trades.db")
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
     config.example.yaml to consumer\\config.yaml, set
     role: consumer + the feed url/token, and add a consumer
     entry with their token to the info server's config
"""
    )


if __name__ == "__main__":
    main()
