"""Clean start: wipe past alerts from the database and clear logs.

Stops the dashboard starting with months of old signal/trade
history after a reset. Paper ledgers, mirror markers, and all
other state (config, positions, account meta) are preserved.

Usage (stop the pipeline and reader first):
    python scripts/clean_start.py           # asks to confirm
    python scripts/clean_start.py --yes     # no confirmation
"""

import argparse
import glob
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def clear_database(db_path: str) -> dict:
    # autocommit mode so VACUUM can run outside a transaction
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    try:
        counts = {}
        for table in ("signals", "trades"):
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                counts[table] = None  # table missing - nothing to do
        conn.execute("VACUUM")
        return counts
    finally:
        conn.close()


def clear_logs(paths) -> list:
    removed = []
    for path in paths:
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError as e:
                print(f"  could not remove {path}: {e}")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.path.join(REPO_ROOT, "trades.db"),
        help="path to trades.db (default: trades.db in repo root)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="skip the confirmation prompt",
    )
    args = parser.parse_args()

    log_files = []
    for pattern in ("pipeline.log*", "reader.log*"):
        log_files.extend(
            sorted(glob.glob(os.path.join(REPO_ROOT, pattern)))
        )
        # logs also live next to the db (pipeline.log follows the db)
        log_files.extend(
            sorted(glob.glob(os.path.join(os.path.dirname(
                os.path.abspath(args.db)), pattern)))
        )

    if not os.path.exists(args.db):
        print(f"database not found: {args.db}")
        return 1

    print(f"database : {args.db}")
    print(f"logs     : {len(set(log_files))} file(s)")

    if not args.yes:
        answer = input(
            "\nThis deletes all recorded signals, the trade log, and "
            "log files.\nPaper ledgers, positions and config are kept. "
            "The reader's seen-messages marker is kept, so old "
            "Discord messages will NOT re-trigger alerts.\n"
            "Make sure the pipeline and reader are stopped.\n\n"
            "Proceed? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted - nothing was changed")
            return 0

    counts = clear_database(args.db)
    for table, n in counts.items():
        if n is None:
            print(f"  {table}: table missing (skipped)")
        else:
            print(f"  {table}: cleared {n} row(s)")
    for path in sorted(set(clear_logs(set(log_files)))):
        print(f"  removed {os.path.basename(path)}")
    print("done - clean slate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
