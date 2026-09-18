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
                CREATE TABLE IF NOT EXISTS positions (
                    mode TEXT NOT NULL,
                    contract_key TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expiry TEXT,
                    strike REAL,
                    right TEXT,
                    qty INTEGER NOT NULL DEFAULT 0,
                    updated_ts TEXT,
                    PRIMARY KEY (mode, contract_key)
                );
                """
            )
            try:
                self._conn.execute("ALTER TABLE trades ADD COLUMN dedupe_key TEXT")
            except sqlite3.OperationalError:
                pass

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
                "SELECT COUNT(*) FROM trades WHERE ts >= ? AND mode = ? "
                "AND status = 'executed' AND action = 'BUY'",
                (midnight, mode),
            ).fetchone()
            return row[0] if row else 0

    def last_buy_time(self) -> str:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT ts FROM trades WHERE action = 'BUY' AND status = 'executed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

    def recent_trade(self, dedupe_key: str, window_minutes: int):
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat(timespec="seconds")
        with self._lock, self._conn:
            return self._conn.execute(
                "SELECT 1 FROM trades WHERE dedupe_key = ? AND ts >= ? "
                "AND status = 'executed' LIMIT 1",
                (dedupe_key, cutoff),
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
                "stop_loss, take_profit, status, detail, message_key, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._now(), mode, action, ticker, qty, price,
                    alert.entry, alert.stop_loss, alert.take_profit,
                    status, detail, message_key, alert.dedupe_key(),
                ),
            )

    def get_position(self, mode: str, contract_key: str) -> int:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT qty FROM positions WHERE mode = ? AND contract_key = ?",
                (mode, contract_key),
            ).fetchone()
            return int(row[0]) if row else 0

    def apply_position(self, mode: str, alert, delta: int):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO positions (mode, contract_key, underlying, expiry, "
                "strike, right, qty, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mode, contract_key) DO UPDATE SET "
                "qty = MAX(0, positions.qty + excluded.qty), "
                "updated_ts = excluded.updated_ts",
                (
                    mode, alert.contract_key(), alert.underlying, alert.expiry,
                    alert.strike, alert.right, delta, self._now(),
                ),
            )
