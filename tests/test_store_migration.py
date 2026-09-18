

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
