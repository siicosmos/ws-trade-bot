import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone


class Store:
    def __init__(self, path: str = "trades.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        # positions cache: bumped on every write so reads between
        # trades are served from memory
        self._positions_version = 0
        self._positions_cache = {}
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
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
                    parsed INTEGER NOT NULL DEFAULT 0,
                    correction INTEGER NOT NULL DEFAULT 0,
                    channel TEXT,
                    received_ts TEXT
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
                    account TEXT NOT NULL DEFAULT 'default',
                    contract_key TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expiry TEXT,
                    strike REAL,
                    right TEXT,
                    qty INTEGER NOT NULL DEFAULT 0,
                    updated_ts TEXT,
                    avg_premium REAL,
                    PRIMARY KEY (mode, account, contract_key)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            try:
                self._conn.execute("ALTER TABLE trades ADD COLUMN dedupe_key TEXT")
            except sqlite3.OperationalError:
                pass
            for col, decl in (
                ("strike", "REAL"), ("expiry", "TEXT"),
                ("opt_right", "TEXT"),
            ):
                try:
                    self._conn.execute(
                        f"ALTER TABLE trades ADD COLUMN {col} {decl}"
                    )
                except sqlite3.OperationalError:
                    pass
            try:
                self._conn.execute(
                    "ALTER TABLE signals ADD COLUMN correction "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass
            try:
                self._conn.execute(
                    "ALTER TABLE signals ADD COLUMN channel TEXT"
                )
            except sqlite3.OperationalError:
                pass
            try:
                self._conn.execute(
                    "ALTER TABLE signals ADD COLUMN received_ts TEXT"
                )
            except sqlite3.OperationalError:
                pass
            for col in ("realized", "peak_bid"):
                try:
                    self._conn.execute(
                        f"ALTER TABLE positions ADD COLUMN {col} REAL"
                    )
                except sqlite3.OperationalError:
                    pass

            cols = [
                r[1] for r in self._conn.execute("PRAGMA table_info(positions)")
            ]
            if "account" not in cols and cols:
                self._conn.executescript(
                    """
                    BEGIN;
                    CREATE TABLE positions_new (
                        mode TEXT NOT NULL,
                        account TEXT NOT NULL DEFAULT 'default',
                        contract_key TEXT NOT NULL,
                        underlying TEXT NOT NULL,
                        expiry TEXT,
                        strike REAL,
                        right TEXT,
                        qty INTEGER NOT NULL DEFAULT 0,
                        updated_ts TEXT,
                        avg_premium REAL,
                        PRIMARY KEY (mode, account, contract_key)
                    );
                    INSERT INTO positions_new
                        (mode, account, contract_key, underlying, expiry, strike,
                         right, qty, updated_ts, avg_premium)
                    SELECT mode, 'default', contract_key, underlying, expiry,
                           strike, right, qty, updated_ts, avg_premium
                    FROM positions;
                    DROP TABLE positions;
                    ALTER TABLE positions_new RENAME TO positions;
                    COMMIT;
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

    def record_signal(self, message_key: str, author: str, text: str, parsed: bool,
                      correction: bool = False, channel: str = "",
                      ts_epoch=None, parsed_epoch=None):
        # both stored in UTC: the alert's own (Discord-displayed) time
        # and the moment the reader parsed and passed it down
        ts = (
            datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
            .isoformat(timespec="seconds")
            if ts_epoch else self._now()
        )
        received = (
            datetime.fromtimestamp(parsed_epoch, tz=timezone.utc)
            .isoformat(timespec="seconds")
            if parsed_epoch else None
        )
        # everything is stored in UTC; the browser renders it in the
        # user's timezone
        ts = (
            datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
            .isoformat(timespec="seconds")
            if ts_epoch else self._now()
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO signals "
                "(message_key, ts, author, text, parsed, correction, "
                "channel, received_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message_key, ts, author, text[:2000], int(parsed),
                 int(correction), channel[:80], received),
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
                "stop_loss, take_profit, status, detail, message_key, dedupe_key, "
                "strike, expiry, opt_right) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._now(), mode, action, ticker, qty, price,
                    alert.entry, alert.stop_loss, alert.take_profit,
                    status, detail, message_key, alert.dedupe_key(),
                    getattr(alert, "strike", None),
                    getattr(alert, "expiry", None),
                    getattr(alert, "right", None),
                ),
            )

    def last_buy_contract(self, ticker, expiry):
        """Strike/right of the most recent BUY for this underlying
        and expiry, for sell-mismatch detection."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT strike, opt_right FROM trades "
                "WHERE action = 'BUY' AND ticker = ? AND expiry = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (ticker, expiry),
            ).fetchone()
        if not row or row[0] is None:
            return None
        return {"strike": row[0], "right": row[1] or "?"}

    def get_position(self, mode: str, contract_key: str, account="default") -> int:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT qty FROM positions WHERE mode = ? AND account = ? "
                "AND contract_key = ?",
                (mode, account, contract_key),
            ).fetchone()
            return int(row[0]) if row else 0

    def list_positions(self, mode: str, account=None):
        keys = ["account", "contract_key", "underlying", "expiry",
                "strike", "right", "qty", "avg_premium", "realized",
                "peak_bid"]
        cache_key = (mode, account)
        with self._lock:
            cached = self._positions_cache.get(cache_key)
            if cached and cached[0] == self._positions_version:
                return [dict(r) for r in cached[1]]
            query = (
                "SELECT account, contract_key, underlying, expiry, "
                "strike, right, qty, avg_premium, realized, peak_bid "
                "FROM positions WHERE mode = ? AND qty > 0"
            )
            params = [mode]
            if account is not None:
                query += " AND account = ?"
                params.append(account)
            query += " ORDER BY account, updated_ts DESC"
            rows = [
                dict(zip(keys, r))
                for r in self._conn.execute(query, params).fetchall()
            ]
            self._positions_cache[cache_key] = (
                self._positions_version, rows
            )
            return [dict(r) for r in rows]

    def apply_position(
        self, mode: str, alert, delta: int, premium=None, account="default"
    ):
        key = alert.contract_key()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT qty, avg_premium, realized FROM positions "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                (mode, account, key),
            ).fetchone()
            if row is None:
                old_qty, old_avg, old_realized = 0, None, 0.0
            else:
                old_qty, old_avg, old_realized = int(row[0]), row[1], row[2] or 0.0

            self._positions_version += 1
            self._positions_cache.clear()
            new_qty = max(0, old_qty + delta)
            new_avg = old_avg
            realized = old_realized
            if delta > 0 and premium is not None:
                total_old = old_qty * (old_avg or 0.0)
                new_avg = (
                    (total_old + delta * premium) / new_qty if new_qty else old_avg
                )
            elif delta < 0 and premium is not None and old_avg:
                realized = old_realized + (-delta) * (premium - old_avg) * 100

            self._conn.execute(
                "INSERT INTO positions (mode, account, contract_key, underlying, "
                "expiry, strike, right, qty, updated_ts, avg_premium, realized) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mode, account, contract_key) DO UPDATE SET "
                "qty = excluded.qty, updated_ts = excluded.updated_ts, "
                "avg_premium = excluded.avg_premium, realized = excluded.realized",
                (
                    mode, account, key, alert.underlying, alert.expiry,
                    alert.strike, alert.right, new_qty, self._now(), new_avg,
                    realized,
                ),
            )

            if old_qty > 0 and new_qty == 0:
                self._record_close(mode, realized)

    def _record_close(self, mode: str, realized: float):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        streak = self._get_streak_unlocked(mode)
        if streak["date"] != today:
            streak = {"count": 0, "date": today}
        if realized < 0:
            streak["count"] += 1
        else:
            streak["count"] = 0
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (
                    f"loss_streak:{mode}",
                    json.dumps(streak),
                ),
            )

    def _get_streak_unlocked(self, mode: str) -> dict:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (f"loss_streak:{mode}",),
        ).fetchone()
        if not row:
            return {"count": 0, "date": ""}
        try:
            data = json.loads(row[0])
            return data if isinstance(data, dict) else {"count": 0, "date": ""}
        except (ValueError, TypeError):
            return {"count": 0, "date": ""}

    def _get_streak(self, mode: str) -> dict:
        with self._lock:
            return self._get_streak_unlocked(mode)

    def loss_streak(self, mode: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        streak = self._get_streak(mode)
        if streak["date"] != today:
            return 0
        return int(streak.get("count") or 0)

    def update_peak_bid(self, mode, contract_key, bid, account="default"):
        self._positions_version += 1
        self._positions_cache.clear()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE positions SET peak_bid = MAX("
                "COALESCE(peak_bid, COALESCE(avg_premium, 0)), ?) "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                (bid, mode, account, contract_key),
            )

    def open_risk(self, mode: str, account=None) -> float:
        query = (
            "SELECT SUM(qty * COALESCE(avg_premium, 0) * 100) "
            "FROM positions WHERE mode = ? AND qty > 0"
        )
        params = [mode]
        if account is not None:
            query += " AND account = ?"
            params.append(account)
        with self._lock, self._conn:
            row = self._conn.execute(query, params).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def recent_trades(self, limit=50):
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT ts, mode, action, ticker, qty, price, status, detail "
                "FROM trades ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ["ts", "mode", "action", "ticker", "qty", "price", "status",
                "detail"]
        return [dict(zip(keys, r)) for r in rows]

    def has_recent_prefix(self, text, within_seconds=300, limit=50):
        """True when a recent signal's text is a prefix of this one
        (or vice versa) - discord re-renders a message when someone
        reacts to it, appending the reaction to the text we read.
        """
        norm = re.sub(r"\s+", " ", str(text or "")).strip()
        if not norm:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=within_seconds
        )
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT ts, text FROM signals "
                "ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        for ts, other in rows:
            try:
                when = datetime.fromisoformat(ts)
            except (TypeError, ValueError):
                continue
            if when < cutoff:
                continue
            norm_other = re.sub(r"\s+", " ", str(other or "")).strip()
            if len(norm_other) < 12 or norm_other == norm:
                continue   # too short to be a reaction re-read
            if norm.startswith(norm_other) or norm_other.startswith(norm):
                return True
        return False

    def recent_signals(self, limit=50):
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT ts, text, parsed, correction, channel, received_ts "
                "FROM signals ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ["ts", "text", "parsed", "correction", "channel",
                "received_ts"]
        return [dict(zip(keys, r)) for r in rows]

    def get_cached_value(self, label: str):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (f"ws_value:{label}",),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or "value" not in data:
            return None
        return data

    def set_cached_value(self, label: str, value: float, ts: str):
        payload = json.dumps({"value": float(value), "ts": ts})
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"ws_value:{label}", payload),
            )

    def meta_get(self, key: str, default=None):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else default

    def meta_set(self, key: str, value):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def seed_position(
        self, mode, account, contract_key, underlying, expiry,
        strike, right, qty, avg_premium,
    ):
        """Insert a seeded position row (used to mirror live
        holdings into the paper ledger)."""
        self._positions_version += 1
        self._positions_cache.clear()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO positions (mode, account, contract_key, "
                "underlying, expiry, strike, right, qty, updated_ts, "
                "avg_premium, realized, peak_bid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0) "
                "ON CONFLICT(mode, account, contract_key) DO UPDATE SET "
                "qty = excluded.qty, avg_premium = excluded.avg_premium, "
                "updated_ts = excluded.updated_ts",
                (
                    mode, account, contract_key, underlying, expiry,
                    strike, right, int(qty), self._now(), avg_premium,
                ),
            )

    def paper_equity(self, label: str = "default"):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (f"paper_equity:{label}",),
            ).fetchone()
            return float(row[0]) if row else None

    def set_paper_equity(self, value: float, label: str = "default"):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"paper_equity:{label}", str(value)),
            )

    def adjust_paper_equity(self, delta: float, label: str = "default"):
        current = self.paper_equity(label)
        if current is None:
            return
        self.set_paper_equity(current + delta, label)
