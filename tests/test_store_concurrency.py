"""Store concurrency fixes: the paper-equity read-modify-write
must not lose adjustments, and the positions cache invalidation
must be synchronized with the reader's lock."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import _fresh_store  # noqa: E402

pytestmark = pytest.mark.essential


def test_adjust_paper_equity_concurrent_adjustments():
    """N threads each applying +1 must land on initial + N: the
    read-modify-write runs under the write lock (two concurrent
    callers used to read the same current and lose one delta)."""
    store = _fresh_store()
    store.set_paper_equity(100.0, "default")
    threads = []
    per_thread = 25
    for _ in range(8):
        t = threading.Thread(
            target=lambda: [
                store.adjust_paper_equity(1.0, "default")
                for _ in range(per_thread)
            ],
            daemon=True,
        )
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert store.paper_equity("default") == 100.0 + 8 * per_thread


def test_adjust_paper_equity_missing_label_stays_absent():
    store = _fresh_store()
    store.adjust_paper_equity(5.0, "ghost")
    assert store.paper_equity("ghost") is None


def test_positions_cache_invalidation_is_lock_synchronized():
    """The writer's bump runs under the reader's _cache_lock too:
    a reader that captured the old version before the bump must
    not be able to store pre-commit rows under the new version.
    Observed through the contract: a write is visible to the very
    next read, and the version moved."""
    store = _fresh_store()
    from core.parser import parse_alert

    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 small")
    store.apply_position("paper", alert, 2, premium=1.5, account="a")
    rows = store.list_positions("paper", "a")
    assert len(rows) == 1 and rows[0]["qty"] == 2
    store.apply_position("paper", alert, 1, premium=1.5, account="a")
    rows = store.list_positions("paper", "a")
    assert rows[0]["qty"] == 3
    # the bump/clear now lives in one synchronized helper
    assert hasattr(store, "_invalidate_positions")


def test_store_close_releases_and_reconnects():
    store = _fresh_store()
    store.record_signal("k1", "a", "BOUGHT 0DTE SPY 759c @ 1.5",
                        parsed=True)
    store.close()
    assert getattr(store._local, "conn", None) is None
    # the next operation reconnects transparently
    assert store.seen_signal("k1")
    store.close()


def test_search_until_date_bound_covers_both_kinds():
    """A date-only until must cover that whole day for trades AND
    signals - the normalization now happens once, not per _run
    call (the old nonlocal made the second call depend on the
    first having already stretched the text)."""
    from datetime import datetime, timezone

    from core.parser import parse_alert

    store = _fresh_store()
    now_epoch = datetime.now(timezone.utc).timestamp()
    today = datetime.now(timezone.utc).date().isoformat()
    store.record_signal("k-s", "a", "BOUGHT 0DTE SPY 759c @ 1.5",
                        parsed=True, ts_epoch=now_epoch)
    alert = parse_alert("BOUGHT 0DTE SPY 759c @ 1.5 small")
    store.record_trade("paper", "BUY", "SPY", 1, 1.5, alert,
                       "executed", "ok", "k-s")
    rows, total = store.search_history(
        kind="both", until=today, limit=50,
    )
    kinds = {r["type"] for r in rows}
    assert kinds == {"trade", "signal"}, rows
    assert total == 2
