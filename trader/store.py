import sqlite3
import threading
from datetime import datetime, timedelta, timezone


class Store:
    def __init__(self, path: str = "trades.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    message_key TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    author TEXT,
                    text TEXT,
                    parsed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    action TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    price REAL,
                    entry REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    message_key TEXT
                );
                """
            )

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def seen_signal(self, message_key: str) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM signals WHERE message_key = ?", (message_key,)
            ).fetchone()
            return row is not None

    def record_signal(self, message_key: str, author: str, text: str, parsed: bool):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO signals (message_key, ts, author, text, parsed) "
                "VALUES (?, ?, ?, ?, ?)",
                (message_key, self._now(), author, text[:2000], int(parsed)),
            )

    def trades_today(self, mode: str) -> int:
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(timespec="seconds")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM trades WHERE ts >= ? AND mode = ? AND status = 'executed'",
                (midnight, mode),
            ).fetchone()
            return row[0] if row else 0

    def last_trade_time(self) -> str:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT ts FROM trades ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

    def recent_trade(
        self, ticker: str, action: str, window_minutes: int
    ):
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat(timespec="seconds")
        with self._lock, self._conn:
            return self._conn.execute(
                "SELECT * FROM trades WHERE ticker = ? AND action = ? AND ts >= ? "
                "AND status = 'executed' LIMIT 1",
                (ticker, action, cutoff),
            ).fetchone()

    def record_trade(
        self,
        mode: str,
        action: str,
        ticker: str,
        qty: int,
        price,
        alert,
        status: str,
        detail: str,
        message_key: str = None,
    ):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO trades (ts, mode, action, ticker, qty, price, entry, "
                "stop_loss, take_profit, status, detail, message_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._now(), mode, action, ticker, qty, price,
                    alert.entry, alert.stop_loss, alert.take_profit,
                    status, detail, message_key,
                ),
            )
