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


def test_check_once_pulls_and_restarts(tmp_path):
    import subprocess

    from trader.updater import AutoUpdater, _git

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(cwd, *args):
        subprocess.run(
            ["git", *args], cwd=cwd, env=env,
            capture_output=True, check=True,
        )

    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", str(origin))

    work = tmp_path / "work"
    git(tmp_path, "clone", str(origin), str(work))
    (work / "f.txt").write_text("1")
    git(work, "add", "-A")
    git(work, "commit", "-m", "initial")
    git(work, "push", "-u", "origin", "HEAD")

    calls = []
    up = AutoUpdater(_cfg(interval=600, enabled=True), str(work), "",
                     restart=lambda: calls.append(1))
    assert up.check_once() is False
    assert calls == []

    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    (other / "f.txt").write_text("2")
    git(other, "add", "-A")
    git(other, "commit", "-m", "second")
    git(other, "push")

    assert up.check_once() is True, up.last_result
    assert calls == [1]
    assert (work / "f.txt").read_text() == "2"
    remote_head = _git(str(other), "rev-parse", "HEAD").stdout.strip()
    assert _git(str(work), "rev-parse", "HEAD").stdout.strip() == remote_head


def test_update_record_roundtrip(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    _commit(repo, "a")

    up = AutoUpdater(_cfg(), str(repo), "")
    assert up.last_pull() is None
    up._record_update("manual")
    record = up.last_pull()
    assert record["how"] == "manual"
    assert record["commit"]
    assert record["ts"] > 0
    up._record_update("auto")
    assert up.last_pull()["how"] == "auto"
