"""Entry point for both apps - a thin dispatcher.

Each role folder is a self-contained app (code + <role>.config.yaml
+ <role>.trades.db + start.bat), like reader/:

  info/     the alert source: reader ingest + feed, no trading
  consumer/ the trading app: risk, sizing, paper/live executors

run.py loads the config, cleans up stale instances of the same
config, and hands off to the role's main.
"""

import argparse
import os
import sys

from core.config import load_config

ROOT = os.path.dirname(os.path.abspath(__file__))


def migrate_legacy_db(db_path):
    """One-time rename: trades.db -> <role>.trades.db (the
    per-role db naming). Fires only when the new name is free and
    the old file is still next to it."""
    full = os.path.abspath(db_path)
    old = os.path.join(os.path.dirname(full), "trades.db")
    if full == old or os.path.exists(full) or not os.path.exists(old):
        return
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(old + suffix):
            os.rename(old + suffix, full + suffix)
    print(f"migrated {old} -> {full}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    # a hard restart can leave the previous instance holding
    # the port - clear it before we bind. the config path
    # scopes the match: the info server and the consumer app
    # share run.py but must never kill each other
    from core.ops.processes import terminate_stale_instances

    terminate_stale_instances(
        os.path.abspath(__file__),
        config_path=os.path.abspath(args.config),
    )

    cfg = load_config(args.config)
    role = getattr(cfg.pipeline, "role", "consumer") or "consumer"

    # the db name follows the role: consumer.trades.db /
    # info.trades.db (an explicit --db always wins)
    if not args.db:
        args.db = f"{role}.trades.db"
        migrate_legacy_db(args.db)

    if role == "info":
        from info.server import main as info_main

        info_main(cfg, args)
    else:
        from consumer.app import main as consumer_main

        consumer_main(cfg, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped")