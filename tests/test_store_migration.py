

def test_old_db_gets_received_ts_column(tmp_path):
    # a database created before received_ts existed must migrate at
    # init, not on first write - /api/signals runs before any alert
    import sqlite3

    from core.store import Store

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE signals (message_key TEXT PRIMARY KEY, ts TEXT, "
        "author TEXT, text TEXT, parsed INTEGER, correction INTEGER, "
        "channel TEXT)"
    )
    conn.commit()
    conn.close()

    store = Store(str(db))
    rows = store.recent_signals()
    assert rows == []


def test_old_trades_table_gets_message_key(tmp_path):
    """The message_key index made a pre-linking database crash at
    init (no such column) - the column must migrate like the
    other trades additions."""
    import sqlite3

    from core.store import Store

    db = tmp_path / "old_trades.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT NOT NULL, mode TEXT NOT NULL, action TEXT NOT NULL, "
        "ticker TEXT NOT NULL, qty INTEGER NOT NULL, price REAL, "
        "entry REAL, stop_loss REAL, take_profit REAL, "
        "status TEXT NOT NULL, detail TEXT, dedupe_key TEXT)"
    )
    conn.commit()
    conn.close()

    store = Store(str(db))
    rows, total = store.search_history(kind="both")
    assert total == 0


def _seed_raw_positions(conn, rows):
    for row in rows:
        conn.execute(
            "INSERT INTO positions (mode, account, contract_key, "
            "underlying, expiry, strike, right, qty, updated_ts, "
            "avg_premium) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )


def test_timestamp_expiry_keys_are_healed(tmp_path):
    """positions seeded from live graphql rows carry the full
    expiry timestamp in expiry + contract_key; the quote map
    keys the plain date - schema init rewrites those rows so
    the paper price matches the real account's quote."""
    import sqlite3

    from core.store import Store

    db = tmp_path / "ts_expiry.db"
    store = Store(str(db))
    conn = sqlite3.connect(str(db))
    _seed_raw_positions(conn, [(
        "paper", "A",
        "SPX-2026-09-25T00:00:00.000-04:00-6000-C",
        "SPX", "2026-09-25T00:00:00.000-04:00", 6000.0, "C",
        2, "ts", 5.0,
    )])
    conn.commit()
    conn.close()

    healed = Store(str(db))
    rows = healed.list_positions("paper", "A")
    assert len(rows) == 1
    assert rows[0]["contract_key"] == "SPX-2026-09-25-6000-C"
    assert rows[0]["expiry"] == "2026-09-25"
    assert rows[0]["qty"] == 2
    assert rows[0]["avg_premium"] == 5.0


def test_timestamp_expiry_keys_merge_with_plain_date_rows(tmp_path):
    """a seeded timestamp-key row for a contract the alert path
    already tracked under the plain date must not break init or
    duplicate the position - the rows merge."""
    import sqlite3

    from core.store import Store

    db = tmp_path / "ts_expiry_merge.db"
    store = Store(str(db))
    conn = sqlite3.connect(str(db))
    _seed_raw_positions(conn, [
        (
            "paper", "A",
            "SPX-2026-09-25T00:00:00.000-04:00-6000-C",
            "SPX", "2026-09-25T00:00:00.000-04:00", 6000.0, "C",
            2, "ts", 5.0,
        ),
        (
            "paper", "A",
            "SPX-2026-09-25-6000-C",
            "SPX", "2026-09-25", 6000.0, "C",
            1, "ts2", 4.0,
        ),
    ])
    conn.commit()
    conn.close()

    healed = Store(str(db))
    rows = healed.list_positions("paper", "A")
    assert len(rows) == 1
    assert rows[0]["contract_key"] == "SPX-2026-09-25-6000-C"
    assert rows[0]["qty"] == 3
    assert rows[0]["avg_premium"] == (2 * 5.0 + 1 * 4.0) / 3
