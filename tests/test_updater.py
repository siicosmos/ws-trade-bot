import os
import subprocess

from trader.updater import AutoUpdater


def _cfg(interval=600, enabled=False):
    return type(
        "C", (), {"auto_update": type(
            "A", (), {"interval_seconds": interval, "enabled": enabled}
        )()},
    )()


def _commit(repo, msg):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", msg],
        cwd=repo, env=env, capture_output=True, check=True,
    )


def test_updater_detects_local_head_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    _commit(repo, "a")

    up = AutoUpdater(_cfg(), str(repo), "")
    assert not up._local_head_changed()

    _commit(repo, "b")
    assert up._local_head_changed()


def test_updater_ignores_head_change_when_not_a_repo(tmp_path):
    up = AutoUpdater(_cfg(), str(tmp_path), "")
    assert not up._local_head_changed()


def test_updater_restart_on_local_change(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    _commit(repo, "a")

    calls = []
    up = AutoUpdater(_cfg(), str(repo), "", restart=lambda: calls.append(1))
    _commit(repo, "b")
    up._restart_for_local_change()
    assert calls == [1]
    assert "local change" in up.last_result
