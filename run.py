"""Entry point for both apps - a thin dispatcher.

State lives apart from code so an update swap can never touch it:

  config/   the live configs (config/<role>.config.yaml) + the
            shipped examples (<role>.example.config.yaml)
  db/       the ledgers (<role>.trades.db); the reader's
            seen-set lives in reader/

run.py loads the config, cleans up stale instances of the same
config, and hands off to the role's main.
"""

import argparse
import os
import sys

from core.config import load_config

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", required=True)
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

    # a new release's config knobs land in the live config
    # before the load: the example documents every key (with
    # comments) - the missing ones are appended add-only, so the
    # knobs are visible and tunable instead of silently defaulting
    from core.ops.config_merge import merge_new_config_keys

    example = os.path.join(
        os.path.dirname(os.path.abspath(args.config)),
        os.path.basename(args.config).replace(
            ".config.yaml", ".example.config.yaml"
        ),
    )
    try:
        added = merge_new_config_keys(
            os.path.abspath(args.config), example
        )
        if added:
            print(
                "config: added " + str(len(added))
                + " new key(s) from the example: " + ", ".join(added)
            )
    except OSError as e:
        print(f"config: the new-key merge failed: {e}")

    cfg = load_config(args.config)
    role = getattr(cfg.pipeline, "role", "consumer") or "consumer"

    # the db name follows the role, in db/ - state lives apart
    # from the code dirs an update swap replaces (an explicit
    # --db always wins)
    if not args.db:
        db_dir = os.path.join(ROOT, "db")
        os.makedirs(db_dir, exist_ok=True)
        args.db = os.path.join(db_dir, f"{role}.trades.db")
    else:
        # an explicit --db creates its parent dir too (the
        # default path is pre-created above)
        parent = os.path.dirname(os.path.abspath(args.db))
        if parent:
            os.makedirs(parent, exist_ok=True)

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