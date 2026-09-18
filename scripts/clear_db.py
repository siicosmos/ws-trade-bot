import argparse
import os
import sqlite3

TABLES = ("signals", "trades", "positions", "meta")


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(
        description="Clear all bot data (signals, trades, positions, "
        "paper ledgers, cached account values). Stop the pipeline first."
    )
    ap.add_argument(
        "--db", default=os.path.join(repo, "trades.db"),
    )
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"{args.db} not found - nothing to clear")
        return

    conn = sqlite3.connect(args.db)
    counts = {}
    for table in TABLES:
        try:
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = None

    print(f"database: {args.db}")
    for table in TABLES:
        n = counts[table]
        print(f"  {table}: {'(missing)' if n is None else f'{n} rows'}")

    if not args.yes:
        answer = input("delete all of the above? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            conn.close()
            return

    for table in TABLES:
        if counts[table] is None:
            continue
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.isolation_level = None
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError:
        pass
    conn.close()
    print("database cleared")


if __name__ == "__main__":
    main()
