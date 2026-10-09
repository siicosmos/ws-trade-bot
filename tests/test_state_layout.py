"""The state-file layout pins: per-role update records, the
root session key, the per-role exit files, the history kind
fallback."""

import os

import core.web_common as wc


def test_update_records_are_per_role(tmp_path):
    from core.ops.updater import update_record_path

    assert update_record_path(str(tmp_path), "info") == (
        os.path.join(str(tmp_path), ".last_update_info.json")
    )
    assert update_record_path(str(tmp_path), "consumer") == (
        os.path.join(str(tmp_path), ".last_update_consumer.json")
    )


def test_session_key_lives_at_the_repo_root(tmp_path, monkeypatch):
    """the login-session signing key is created at the repo
    root (outside the swapped code dirs) and reused across
    restarts - a key inside a swapped dir would log everyone
    out on every update."""
    key_file = tmp_path / ".consumer_session_key"
    monkeypatch.setattr(wc, "_session_key_file",
                        lambda: str(key_file))
    k1 = wc.load_secret_key(str(tmp_path / "config" / "x.yaml"))
    assert key_file.exists()
    k2 = wc.load_secret_key(str(tmp_path / "config" / "x.yaml"))
    assert k1 == k2
    # not inside the consumer folder (the pre-refactor home)
    assert not (tmp_path / "consumer" / ".consumer_session_key").exists()


def test_exit_files_are_per_role():
    """the crash-restart exit markers are keyed by role (the
    shared file the old layout used collided)."""
    import consumer.app as capp
    import info.server as iserver

    assert "pipeline_exit_consumer.txt" in capp.EXIT_FILE
    assert "pipeline_exit_info.txt" in iserver.EXIT_FILE


def test_history_search_unknown_kind_falls_back_to_trades(tmp_path):
    from core.parser import parse_alert
    from core.store import Store

    store = Store(str(tmp_path / "t.db"))
    alert = parse_alert("BOUGHT 09/25 COIN 210c @ 2.0")
    store.record_signal("k1", "a", "BOUGHT 09/25 COIN 210c @ 2.0",
                        parsed=True)
    store.record_trade("paper", alert.action, alert.ticker, 1, 2.0,
                       alert, "executed", "t")
    rows, total = store.search_history(kind="bogus")
    assert total == 1          # the trades table
    assert rows[0]["type"] == "trade"
    rows, total = store.search_history(kind="signals")
    assert total == 1
    assert rows[0]["type"] == "signal"
