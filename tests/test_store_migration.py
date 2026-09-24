

def test_old_db_gets_received_ts_column(tmp_path):
    # a database created before received_ts existed must migrate at
    # init, not on first write - /api/signals runs before any alert
    import sqlite3

    from trader.store import Store

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

    from trader.store import Store

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
