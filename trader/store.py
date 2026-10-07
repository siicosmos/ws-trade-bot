import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone


class Store:
    def __init__(self, path: str = "trades.db", retention_days=90):
        self.path = path
        self.retention_days = retention_days
        self._prune_day = None
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        # positions cache: bumped on every write so reads between
        # trades are served from memory
        self._positions_version = 0
        self._positions_cache = {}
        # data version: bumped on any write that feeds the
        # dashboard summaries, so caches invalidate immediately
        self._data_version = 0
        self._init_schema()

    @property
    def _conn(self):
        """Per-thread connection: WAL lets readers run alongside
        the writer without a global lock (plan #9)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path, check_same_thread=False
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_schema(self):
        with self._write_lock, self._conn:
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
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer',
                    created_ts TEXT,
                    last_login_ts TEXT
                );
                """
            )
            try:
                self._conn.execute("ALTER TABLE trades ADD COLUMN dedupe_key TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                self._conn.execute(
                    "ALTER TABLE trades ADD COLUMN message_key TEXT"
                )
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
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_trades_mode_ts
                    ON trades (mode, ts);
                CREATE INDEX IF NOT EXISTS idx_trades_dedupe
                    ON trades (dedupe_key, ts);
                CREATE INDEX IF NOT EXISTS idx_trades_message_key
                    ON trades (message_key);
                """
            )
            for col in ("realized", "peak_bid", "tp_gain_pct",
                        "trail_pct"):
                try:
                    self._conn.execute(
                        f"ALTER TABLE positions ADD COLUMN {col} REAL"
                    )
                except sqlite3.OperationalError:
                    pass
            # the alert's size keyword rides the position row: the
            # stop monitor prices per-size stop losses off it
            try:
                self._conn.execute(
                    "ALTER TABLE positions ADD COLUMN size TEXT"
                )
            except sqlite3.OperationalError:
                pass
            # live orders wait here until the mirror thread
            # reconciles them against the actual ws fill - an
            # estimated booking that never fills gets reversed.
            # pre_qty/pre_avg snapshot the position before the
            # estimate so corrections restore the exact basis
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pending_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    account TEXT NOT NULL,
                    order_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'option',
                    contract_key TEXT NOT NULL,
                    underlying TEXT NOT NULL,
                    expiry TEXT,
                    strike REAL,
                    opt_right TEXT,
                    action TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    est_price REAL,
                    pre_qty INTEGER NOT NULL DEFAULT 0,
                    pre_avg REAL,
                    placed_ts TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                );
                CREATE INDEX IF NOT EXISTS idx_pending_orders_open
                    ON pending_orders (mode, account, status, placed_ts);
                """
            )
            # partial-fill tracking on open orders: the fills
            # accumulate (qty + blended price) until the order
            # completes or the sweep settles the remainder
            for col, decl in (
                ("filled_qty", "INTEGER DEFAULT 0"),
                ("filled_price", "REAL"),
            ):
                try:
                    self._conn.execute(
                        f"ALTER TABLE pending_orders ADD COLUMN {col} {decl}"
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
                        (mode, account, contract_key, underlying, expiry,
                         strike, right, qty, updated_ts, avg_premium)
                    SELECT mode, 'default', contract_key, underlying, expiry,
                           strike, right, qty, updated_ts, avg_premium
                    FROM positions;
                    DROP TABLE positions;
                    ALTER TABLE positions_new RENAME TO positions;
                    COMMIT;
                    """
                )
            # heal option rows keyed by the raw graphql expiry
            # timestamp: the quote map (and every alert) keys the
            # plain date, so those rows never matched a live quote
            # and froze at cost basis
            rows = self._conn.execute(
                "SELECT rowid, mode, account, contract_key, underlying, "
                "expiry, strike, right, qty, updated_ts, avg_premium, "
                "realized, peak_bid FROM positions "
                "WHERE expiry IS NOT NULL AND strike IS NOT NULL "
                "AND right IS NOT NULL AND length(expiry) > 10"
            ).fetchall()
            for row in rows:
                (rowid, mode, account, _key, underlying, expiry,
                 strike, right, qty, updated_ts, avg_premium,
                 realized, peak_bid) = row
                try:
                    new_key = (
                        f"{underlying}-{str(expiry)[:10]}"
                        f"-{float(strike):g}-{right}"
                    )
                except (TypeError, ValueError):
                    continue
                clash = self._conn.execute(
                    "SELECT rowid, qty, avg_premium, realized, peak_bid, "
                    "updated_ts FROM positions "
                    "WHERE mode = ? AND account = ? AND contract_key = ?",
                    (mode, account, new_key),
                ).fetchone()
                if clash is None:
                    self._conn.execute(
                        "UPDATE positions SET expiry = ?, "
                        "contract_key = ? WHERE rowid = ?",
                        (str(expiry)[:10], new_key, rowid),
                    )
                    continue
                # a plain-date row for the same contract already
                # exists: fold the timestamp-keyed row into it
                clash_rowid, ex_qty, ex_avg, ex_realized, ex_peak, ex_ts = (
                    clash
                )
                total = int(ex_qty or 0) + int(qty or 0)
                if total:
                    merged_avg = (
                        (int(ex_qty or 0) * (ex_avg or 0.0)
                         + int(qty or 0) * (avg_premium or 0.0))
                        / total
                    )
                else:
                    merged_avg = avg_premium
                self._conn.execute(
                    "UPDATE positions SET qty = ?, avg_premium = ?, "
                    "realized = COALESCE(realized, 0) + ?, "
                    "peak_bid = MAX(COALESCE(peak_bid, 0), ?), "
                    "updated_ts = MAX(COALESCE(updated_ts, ''), ?) "
                    "WHERE rowid = ?",
                    (
                        total, merged_avg if total else avg_premium,
                        realized or 0.0, peak_bid or 0.0,
                        updated_ts or "", clash_rowid,
                    ),
                )
                self._conn.execute(
                    "DELETE FROM positions WHERE rowid = ?", (rowid,)
                )

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def seen_signal(self, message_key: str) -> bool:
        with self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM signals WHERE message_key = ?", (message_key,)
            ).fetchone()
            return row is not None

    def record_signal(self, message_key: str, author: str, text: str, parsed: bool,
                      correction: bool = False, channel: str = "",
                      ts_epoch=None, parsed_epoch=None) -> bool:
        """Record a signal; returns True when the row was newly
        inserted (False = duplicate key). The INSERT OR IGNORE
        under the write lock is the atomic dedupe: two delivery
        paths (push + feed pull) can race on the same message
        and only one wins."""
        self.maybe_prune()
        self._touch()
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
        with self._write_lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO signals "
                "(message_key, ts, author, text, parsed, correction, "
                "channel, received_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message_key, ts, author, text[:2000], int(parsed),
                 int(correction), channel[:80], received),
            )
            return cur.rowcount > 0

    def signals_since(self, since_rowid: int = 0, limit: int = 200):
        """Signals with rowid > since_rowid, oldest first - the
        feed API's cursor window. rowid is the monotonic feed
        position (insertion order)."""
        keys = ("id", "message_key", "ts", "author", "text", "parsed",
                "correction", "channel", "received_ts")
        with self._conn:
            rows = self._conn.execute(
                "SELECT rowid AS id, message_key, ts, author, text, "
                "parsed, correction, channel, received_ts "
                "FROM signals WHERE rowid > ? "
                "ORDER BY rowid LIMIT ?",
                (since_rowid, limit),
            ).fetchall()
        return [dict(zip(keys, r)) for r in rows]

    def max_signal_rowid(self) -> int:
        with self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM signals"
            ).fetchone()
        return int(row[0]) if row else 0

    def maybe_prune(self, now=None):
        """Drop signals/trades older than the retention window,
        once per calendar day. Cheap no-op otherwise."""
        retention = getattr(self, "retention_days", 90)
        if not retention or retention <= 0:
            return
        now = now or datetime.now(timezone.utc)
        today = now.date().isoformat()
        if self._prune_day == today:
            return
        self._prune_day = today
        cutoff = (
            now - timedelta(days=int(retention))
        ).isoformat(timespec="seconds")
        with self._write_lock, self._conn:
            gone_s = self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE ts < ?",
                (cutoff,),
            ).fetchone()[0]
            gone_t = self._conn.execute(
                "SELECT COUNT(*) FROM trades WHERE ts < ?",
                (cutoff,),
            ).fetchone()[0]
            if gone_s or gone_t:
                self._conn.execute(
                    "DELETE FROM signals WHERE ts < ?", (cutoff,)
                )
                self._conn.execute(
                    "DELETE FROM trades WHERE ts < ?", (cutoff,)
                )
                self._touch()
                print(
                    f"history prune: removed {gone_s} signal(s) and "
                    f"{gone_t} trade(s) older than {retention} days"
                )

    def trades_today(self, mode: str) -> int:
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(timespec="seconds")
        with self._conn:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM trades WHERE ts >= ? AND mode = ? "
                "AND status = 'executed' AND action = 'BUY'",
                (midnight, mode),
            ).fetchone()
            return row[0] if row else 0

    def last_buy_time(self) -> str:
        with self._conn:
            row = self._conn.execute(
                "SELECT ts FROM trades WHERE action = 'BUY' AND status = 'executed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

    def recent_trade(self, dedupe_key: str, window_minutes: int):
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat(timespec="seconds")
        with self._conn:
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
        self.maybe_prune()
        self._touch()
        with self._write_lock, self._conn:
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
        with self._conn:
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
        with self._conn:
            row = self._conn.execute(
                "SELECT qty FROM positions WHERE mode = ? AND account = ? "
                "AND contract_key = ?",
                (mode, account, contract_key),
            ).fetchone()
            return int(row[0]) if row else 0

    def list_positions(self, mode: str, account=None):
        keys = ["account", "contract_key", "underlying", "expiry",
                "strike", "right", "qty", "avg_premium", "realized",
                "peak_bid", "size", "tp_gain_pct", "trail_pct"]
        cache_key = (mode, account)
        with self._cache_lock:
            cached = self._positions_cache.get(cache_key)
            if cached and cached[0] == self._positions_version:
                return [dict(r) for r in cached[1]]
            query = (
                "SELECT account, contract_key, underlying, expiry, "
                "strike, right, qty, avg_premium, realized, peak_bid, "
                "size, tp_gain_pct, trail_pct FROM positions "
                "WHERE mode = ? AND qty > 0"
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
        with self._write_lock, self._conn:
            row = self._conn.execute(
                "SELECT qty, avg_premium, realized FROM positions "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                (mode, account, key),
            ).fetchone()
            if row is None:
                old_qty, old_avg, old_realized = 0, None, 0.0
            else:
                old_qty, old_avg, old_realized = int(row[0]), row[1], row[2] or 0.0

            self._touch()
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
                # options settle per-contract (x100), stocks per
                # share (x1) - the old x100-for-everything inflated
                # stock p&l a hundredfold
                mult = 100 if getattr(alert, "kind", "option") == "option" else 1
                realized = old_realized + (-delta) * (premium - old_avg) * mult

            self._conn.execute(
                "INSERT INTO positions (mode, account, contract_key, underlying, "
                "expiry, strike, right, qty, updated_ts, avg_premium, realized, size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mode, account, contract_key) DO UPDATE SET "
                "qty = excluded.qty, updated_ts = excluded.updated_ts, "
                "avg_premium = excluded.avg_premium, realized = excluded.realized, "
                "size = COALESCE(excluded.size, positions.size)",
                (
                    mode, account, key, alert.underlying, alert.expiry,
                    alert.strike, alert.right, new_qty, self._now(), new_avg,
                    realized,
                    getattr(alert, "size", None),
                ),
            )

            # accumulate the realized pnl per day (sells only) -
            # lotto / profits-only sizing is capped by today's
            # realized gain and the account cards show it per label
            if delta < 0 and realized != old_realized:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                delta_realized = round(realized - old_realized, 2)
                for meta_key in (
                    f"realized_today:{mode}:{today}",
                    f"realized_today:{mode}:{account}:{today}",
                ):
                    prev = self._conn.execute(
                        "SELECT value FROM meta WHERE key = ?",
                        (meta_key,),
                    ).fetchone()
                    prev_val = 0.0
                    try:
                        prev_val = (
                            float(json.loads(prev[0])) if prev else 0.0
                        )
                    except (TypeError, ValueError):
                        pass
                    self._conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET "
                        "value = excluded.value",
                        (
                            meta_key,
                            json.dumps(
                                round(
                                    prev_val + delta_realized, 2
                                )
                            ),
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
        with self._write_lock:
            return self._get_streak_unlocked(mode)

    def loss_streak(self, mode: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        streak = self._get_streak(mode)
        if streak["date"] != today:
            return 0
        return int(streak.get("count") or 0)

    def update_peak_bid(self, mode, contract_key, bid, account="default"):
        self._touch()
        self._positions_version += 1
        self._positions_cache.clear()
        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE positions SET peak_bid = MAX("
                "COALESCE(peak_bid, COALESCE(avg_premium, 0)), ?) "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                (bid, mode, account, contract_key),
            )

    def open_risk(self, mode: str, account=None) -> float:
        # option positions only: stocks carry no option-style
        # open risk (and the x100 multiplier is options-specific)
        query = (
            "SELECT SUM(qty * COALESCE(avg_premium, 0) * 100) "
            "FROM positions "
            "WHERE mode = ? AND qty > 0 AND right IS NOT NULL"
        )
        params = [mode]
        if account is not None:
            query += " AND account = ?"
            params.append(account)
        with self._conn:
            row = self._conn.execute(query, params).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def set_position_tp(self, mode: str, account: str,
                        contract_key: str, tp_gain_pct):
        """Per-position take-profit: sell the whole remaining
        position when its gain vs the entry premium reaches this
        percent. None clears the target."""
        self._touch()
        self._positions_version += 1
        self._positions_cache.clear()
        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE positions SET tp_gain_pct = ? "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                (
                    None if tp_gain_pct is None else float(tp_gain_pct),
                    mode, account, contract_key,
                ),
            )

    def set_position_trail(self, mode: str, account: str,
                           contract_key: str, trail_pct):
        """Per-position trailing stop: sell once the bid falls
        this % off its peak (overrides the global trailing stop
        for this position; 0 disables trailing here, None falls
        back to the global setting)."""
        self._touch()
        self._positions_version += 1
        self._positions_cache.clear()
        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE positions SET trail_pct = ? "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                (
                    None if trail_pct is None else float(trail_pct),
                    mode, account, contract_key,
                ),
            )

    def open_risk_clusters(self, mode: str, account=None) -> list:
        """Clustered open risk: option positions grouped by
        (underlying, expiry, right) - three same-direction SPX
        0dte calls are one bet x3, not three positions. Returns
        [{underlying, expiry, right, risk}] largest first."""
        query = (
            "SELECT underlying, expiry, right, "
            "SUM(qty * COALESCE(avg_premium, 0) * 100) AS risk "
            "FROM positions "
            "WHERE mode = ? AND qty > 0 AND right IS NOT NULL"
        )
        params = [mode]
        if account is not None:
            query += " AND account = ?"
            params.append(account)
        query += " GROUP BY underlying, expiry, right ORDER BY risk DESC"
        with self._conn:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "underlying": r[0], "expiry": r[1], "right": r[2],
                "risk": float(r[3] or 0),
            }
            for r in rows if r[3]
        ]

    def signal_text(self, message_key: str):
        """The original alert text for a message key (used to
        recover the size keyword when re-sizing past trades)."""
        if not message_key:
            return None
        with self._conn:
            row = self._conn.execute(
                "SELECT text FROM signals WHERE message_key = ?",
                (message_key,),
            ).fetchone()
        return row[0] if row else None

    def recent_trades(self, limit=50):
        with self._conn:
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
        with self._conn:
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
        with self._conn:
            rows = self._conn.execute(
                "SELECT ts, text, parsed, correction, channel, received_ts "
                "FROM signals ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ["ts", "text", "parsed", "correction", "channel",
                "received_ts"]
        return [dict(zip(keys, r)) for r in rows]

    def search_history(self, kind="trades", ticker=None, action=None,
                       status=None, mode=None, since=None, until=None,
                       q=None, limit=50, offset=0):
        """Search the retained history (plan #12): trades,
        signals, or both merged chronologically - filters apply
        per table (action/status/mode are trade-only). Returns
        (rows, total); every row carries a "type" marker."""
        if kind not in ("trades", "signals", "both"):
            kind = "trades"

        def _run(tk, limit_, offset_):
            def _like(term):
                return (
                    "%" + str(term).replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_") + "%"
                )

            # date-only bounds: since is inclusive by prefix
            # ordering, until is stretched to cover the end date
            nonlocal since, until
            if since:
                since = str(since)
            if until:
                until = str(until)
            if until and len(until) == 10:
                until += "T99"

            where, params = [], []
            if since:
                where.append("ts >= ?")
                params.append(since)
            if until:
                where.append("ts <= ?")
                params.append(until)

            if tk == "signals":
                table = "signals"
                base = ("SELECT ts, author, text, parsed, correction, "
                        "channel, received_ts, message_key "
                        "FROM signals")
                keys = ["ts", "author", "text", "parsed", "correction",
                        "channel", "received_ts", "message_key"]
                order = "rowid DESC"
                if ticker:
                    where.append("UPPER(text) LIKE ? ESCAPE '\\'")
                    params.append(_like(str(ticker).upper()))
                if q:
                    where.append(
                        "(text LIKE ? ESCAPE '\\' "
                        "OR author LIKE ? ESCAPE '\\')"
                    )
                    like = _like(q)
                    params += [like, like]
            else:
                table = "trades"
                base = ("SELECT ts, mode, action, ticker, qty, price, "
                        "status, detail, message_key "
                        "FROM trades")
                keys = ["ts", "mode", "action", "ticker", "qty", "price",
                        "status", "detail", "message_key"]
                order = "id DESC"
                if ticker:
                    where.append("UPPER(ticker) = UPPER(?)")
                    params.append(str(ticker))
                if action:
                    where.append("UPPER(action) = UPPER(?)")
                    params.append(str(action))
                if status:
                    where.append("LOWER(status) = LOWER(?)")
                    params.append(str(status))
                if mode:
                    where.append("mode = ?")
                    params.append(str(mode))
                if q:
                    where.append(
                        "(detail LIKE ? ESCAPE '\\' "
                        "OR ticker LIKE ? ESCAPE '\\')"
                    )
                    like = _like(q)
                    params += [like, like]

            clause = (" WHERE " + " AND ".join(where)) if where else ""
            with self._conn:
                total = self._conn.execute(
                    f"SELECT COUNT(*) FROM {table}" + clause, params
                ).fetchone()[0]
                rows = self._conn.execute(
                    base + clause + f" ORDER BY {order} LIMIT ? OFFSET ?",
                    params + [int(limit_), int(offset_)],
                ).fetchall()
            return [dict(zip(keys, r)) for r in rows], total

        if kind == "both":
            # merged chronological view: alerts and the trades
            # they produced side by side (linked via message_key).
            # An alert's sort key rises to its newest trade's ts
            # so the pair stays adjacent, alert on top - with
            # second-precision timestamps the raw order cannot
            # distinguish them.
            merge_cap = 5000
            trades, t_total = _run("trades", merge_cap, 0)
            signals, s_total = _run("signals", merge_cap, 0)
            for r in trades:
                r["type"] = "trade"
            for r in signals:
                r["type"] = "signal"
            # the alert rides its NEWEST trade's timestamp so the
            # whole group (notify row, paper row, signal) lands
            # together; trades arrive newest-first, so keep the max
            group_top = {}
            for r in trades:
                k = r["message_key"]
                if k:
                    group_top[k] = max(group_top.get(k, ""), r["ts"])

            def _key(r):
                if r["type"] == "signal":
                    top = group_top.get(r["message_key"])
                    ts = top if top and top > r["ts"] else r["ts"]
                    return (ts, 1)   # alert sorts above its trades
                return (r["ts"], 0)

            merged = sorted(
                trades + signals, key=_key, reverse=True,
            )
            page = merged[int(offset):int(offset) + int(limit)]
            return page, t_total + s_total

        rows, total = _run(kind, int(limit), int(offset))
        for r in rows:
            r["type"] = "trade" if kind == "trades" else "signal"
        return rows, total

    def get_cached_value(self, label: str):
        with self._conn:
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
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"ws_value:{label}", payload),
            )

    def data_version(self) -> int:
        """Bumped on every write that can change dashboard data."""
        return self._data_version

    def _touch(self):
        self._data_version += 1

    def meta_get(self, key: str, default=None):
        with self._conn:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else default

    def meta_set(self, key: str, value):
        with self._write_lock, self._conn:
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
        self._touch()
        self._positions_version += 1
        self._positions_cache.clear()
        with self._write_lock, self._conn:
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

    def reset_paper_account(self, label: str = "default"):
        """Drop a paper account's ledger so it can be re-seeded."""
        self._touch()
        self._positions_version += 1
        self._positions_cache.clear()
        with self._write_lock, self._conn:
            self._conn.execute(
                "DELETE FROM positions WHERE mode = 'paper' "
                "AND account = ?", (label,)
            )
            for key in (
                f"paper_seed:{label}", f"paper_initial:{label}",
                f"paper_equity:{label}",
                f"mirror:{label}:since", f"mirror:{label}:ids",
            ):
                self._conn.execute(
                    "DELETE FROM meta WHERE key = ?", (key,)
                )

    def paper_equity(self, label: str = "default"):
        with self._conn:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (f"paper_equity:{label}",),
            ).fetchone()
            return float(row[0]) if row else None

    def set_paper_equity(self, value: float, label: str = "default"):
        self._touch()
        with self._write_lock, self._conn:
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

    # ---- pending live orders (fill reconciliation) ----

    def position_state(self, mode, account, contract_key):
        """(qty, avg_premium) snapshot of a ledger position -
        the pre-order state a pending-order correction restores
        against."""
        with self._conn:
            row = self._conn.execute(
                "SELECT qty, avg_premium FROM positions "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                (mode, account, contract_key),
            ).fetchone()
        return (
            (int(row[0]), row[1]) if row else (0, None)
        )

    def record_pending_order(
        self, mode, account, order_id, kind, contract_key,
        underlying, expiry, strike, opt_right, action, qty,
        est_price, pre_qty=0, pre_avg=None,
    ):
        """A live order just placed: the estimated booking rides
        the positions ledger immediately (gates depend on it);
        this row waits for the mirror to reconcile the actual
        fill or expire the estimate. pre_qty/pre_avg snapshot
        the position before the estimate so the correction can
        restore the exact basis."""
        self._touch()
        with self._write_lock, self._conn:
            self._conn.execute(
                "INSERT INTO pending_orders (mode, account, order_id, "
                "kind, contract_key, underlying, expiry, strike, "
                "opt_right, action, qty, est_price, pre_qty, pre_avg, "
                "placed_ts, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'open')",
                (
                    mode, account, order_id, kind, contract_key,
                    underlying, expiry, strike, opt_right, action,
                    int(qty), est_price, int(pre_qty), pre_avg,
                    self._now(),
                ),
            )

    def open_pending_orders(self, mode, account=None):
        """Open (unreconciled) live orders, oldest first."""
        with self._conn:
            query = (
                "SELECT id, mode, account, order_id, kind, "
                "contract_key, underlying, expiry, strike, "
                "opt_right, action, qty, est_price, pre_qty, "
                "pre_avg, placed_ts, status, filled_qty, "
                "filled_price "
                "FROM pending_orders WHERE mode = ? "
                "AND status = 'open'"
            )
            params = [mode]
            if account is not None:
                query += " AND account = ?"
                params.append(account)
            query += " ORDER BY id"
            return [
                dict(zip(
                    ("id", "mode", "account", "order_id", "kind",
                     "contract_key", "underlying", "expiry", "strike",
                     "opt_right", "action", "qty", "est_price",
                     "pre_qty", "pre_avg", "placed_ts", "status",
                     "filled_qty", "filled_price"),
                    r,
                ))
                for r in self._conn.execute(query, params).fetchall()
            ]

    def update_pending_fill(self, row_id, filled_qty, filled_price):
        """Accumulate an actual fill onto an open order: the
        running filled qty and its blended price. Returns the
        updated totals."""
        self._touch()
        with self._write_lock, self._conn:
            row = self._conn.execute(
                "SELECT filled_qty, filled_price FROM pending_orders "
                "WHERE id = ?", (row_id,),
            ).fetchone()
            prev_qty = int(row[0] or 0) if row else 0
            prev_price = row[1] if row else None
            total = prev_qty + int(filled_qty)
            blended = (
                ((prev_qty * (prev_price or 0.0))
                 + int(filled_qty) * (filled_price or 0.0)) / total
                if total and (filled_price is not None
                              or prev_price is not None)
                else None
            )
            self._conn.execute(
                "UPDATE pending_orders SET filled_qty = ?, "
                "filled_price = ? WHERE id = ?",
                (total, blended, row_id),
            )
            return total, blended

    def settle_pending_order(self, row_id, status):
        self._touch()
        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE pending_orders SET status = ? WHERE id = ?",
                (status, row_id),
            )

    def _bump_realized_today(self, mode, account, delta):
        """Move the realized-today accumulators by a correction
        delta - the same keys apply_position maintains."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for meta_key in (
            f"realized_today:{mode}:{today}",
            f"realized_today:{mode}:{account}:{today}",
        ):
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (meta_key,),
            ).fetchone()
            prev_val = 0.0
            try:
                prev_val = float(json.loads(row[0])) if row else 0.0
            except (TypeError, ValueError):
                pass
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (
                    meta_key,
                    json.dumps(round(prev_val + delta, 2)),
                ),
            )

    def correct_fill(
        self, mode, account, contract_key, action, filled_qty,
        actual_price, pre_qty, pre_avg, booked_qty, booked_price,
        mult=100, underlying="", expiry=None, strike=None,
        opt_right=None,
    ):
        """Reconcile an estimated live booking against the actual
        ws fill, exactly: the pre-order snapshot (pre_qty,
        pre_avg) defines the truth - qty becomes
        pre +/- filled, sells re-price realized against the fill
        (truth minus what the estimate booked), buys re-blend the
        average premium with the actual price."""
        signed_fill = (
            int(filled_qty) if action == "BUY" else -int(filled_qty)
        )
        new_qty = max(0, int(pre_qty) + signed_fill)
        self._touch()
        self._positions_version += 1
        self._positions_cache.clear()
        with self._write_lock, self._conn:
            row = self._conn.execute(
                "SELECT qty, avg_premium, realized FROM positions "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                (mode, account, contract_key),
            ).fetchone()
            if row is None:
                return
            _qty, _avg, realized = (row[0], row[1], row[2] or 0.0)
            sets = ["qty = ?", "updated_ts = ?"]
            params = [new_qty, self._now()]
            if action == "SELL":
                basis = pre_avg or 0.0
                truth = (
                    int(filled_qty) * (actual_price - basis) * mult
                    if actual_price is not None and basis else 0.0
                )
                est = (
                    int(booked_qty) * ((booked_price or 0) - basis)
                    * mult if basis else 0.0
                )
                corr = round(truth - est, 2)
                if corr:
                    sets.append("realized = ?")
                    params.append(round(realized + corr, 2))
                    self._bump_realized_today(mode, account, corr)
            else:   # BUY: re-blend the basis with the actual price
                if filled_qty and actual_price is not None:
                    new_avg = (
                        (int(pre_qty) * (pre_avg or 0.0)
                         + int(filled_qty) * actual_price)
                        / new_qty
                    )
                    sets.append("avg_premium = ?")
                    params.append(round(new_avg, 4))
                else:
                    sets.append("avg_premium = ?")
                    params.append(pre_avg)
            params.extend([mode, account, contract_key])
            self._conn.execute(
                f"UPDATE positions SET {', '.join(sets)} "
                "WHERE mode = ? AND account = ? AND contract_key = ?",
                params,
            )

    def realized_today(self, mode: str, account=None) -> float:
        """Realized pnl booked today (sell gains minus sell
        losses) - the account card shows it per label and the
        lotto / profits-only budget reads the mode aggregate."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (
            f"realized_today:{mode}:{account}:{today}"
            if account
            else f"realized_today:{mode}:{today}"
        )
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,),
        ).fetchone()
        try:
            return float(json.loads(row[0])) if row else 0.0
        except (TypeError, ValueError):
            return 0.0

    # ---- users ----

    def user_count(self) -> int:
        with self._conn:
            return self._conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

    def create_user(self, username: str, password: str,
                    role: str = "viewer") -> bool:
        """Returns False when the username is taken."""
        if not username or not password or role not in (
            "admin", "viewer"
        ):
            return False
        try:
            with self._write_lock, self._conn:
                self._conn.execute(
                    "INSERT INTO users (username, password_hash, "
                    "role, created_ts) VALUES (?, ?, ?, ?)",
                    (
                        username, _hash_password(password), role,
                        self._now(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_user(self, username: str):
        with self._conn:
            row = self._conn.execute(
                "SELECT username, password_hash, role FROM users "
                "WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return {
            "username": row[0],
            "password_hash": row[1],
            "role": row[2],
        }

    def verify_user(self, username: str, password: str):
        """The user dict (without the hash) on a match, else None."""
        user = self.get_user(username)
        if user is None or not _verify_password(
            password, user["password_hash"]
        ):
            return None
        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE users SET last_login_ts = ? "
                "WHERE username = ?",
                (self._now(), username),
            )
        return {"username": user["username"], "role": user["role"]}

    def list_users(self) -> list:
        """No password hashes in listings."""
        with self._conn:
            rows = self._conn.execute(
                "SELECT username, role, created_ts, last_login_ts "
                "FROM users ORDER BY username"
            ).fetchall()
        keys = ["username", "role", "created_ts", "last_login_ts"]
        return [dict(zip(keys, r)) for r in rows]

    def delete_user(self, username: str) -> bool:
        with self._write_lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM users WHERE username = ?", (username,)
            )
            return cur.rowcount > 0

    def update_password(self, username: str, password: str) -> bool:
        if not password:
            return False
        with self._write_lock, self._conn:
            cur = self._conn.execute(
                "UPDATE users SET password_hash = ? "
                "WHERE username = ?",
                (_hash_password(password), username),
            )
            return cur.rowcount > 0

    def update_role(self, username: str, role: str) -> bool:
        if role not in ("admin", "viewer"):
            return False
        with self._write_lock, self._conn:
            cur = self._conn.execute(
                "UPDATE users SET role = ? WHERE username = ?",
                (role, username),
            )
            return cur.rowcount > 0


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 200_000
    )
    return f"pbkdf2$200000${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iters),
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False
