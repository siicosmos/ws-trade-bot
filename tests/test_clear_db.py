import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.parser import parse_alert
from trader.store import Store


def test_clear_db_script():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = Store(path)
    alert = parse_alert("BOUGHT 0DTE SPX 7645c @ .65")
    store.record_signal("k1", "a", "BOUGHT 0DTE SPX 7645c @ .65", True)
    store.record_trade(
        "paper", "BUY", "SPX", 1, 0.65, alert, "executed", "t", "k1"
    )
    assert store.recent_signals(10)
    assert store.trades_today("paper") == 1

    out = subprocess.run(
        [sys.executable, "scripts/clear_db.py", "--db", path, "--yes"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "database cleared" in out.stdout

    assert store.recent_signals(10) == []
    assert store.trades_today("paper") == 0
    os.unlink(path)
