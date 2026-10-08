import os
import subprocess

from core.ops.updater import AutoUpdater


def _cfg(interval=600, enabled=False):
    return type(
        "C", (), {"auto_update": type(
            "A", (), {"interval_seconds": interval, "enabled": enabled}
        )()},
    )()


def _write(repo, path, text):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _git_run(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True
    )


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
    _write(repo, "trader/server.py", "x")
    _git_run(repo, "add", "-A")
    _commit(repo, "a")

    calls = []
    up = AutoUpdater(_cfg(), str(repo), "", restart=lambda *a, **k: calls.append(1))
    _write(repo, "trader/server.py", "y")
    _git_run(repo, "add", "-A")
    _commit(repo, "b")
    up._restart_for_local_change()
    assert calls == [1]

    # a reader-only change must not restart the pipeline
    calls.clear()
    up.start_head = up._head()
    _write(repo, "reader/discord_reader.py", "z")
    _git_run(repo, "add", "-A")
    _commit(repo, "c")
    up._restart_for_local_change()
    assert calls == []
    assert up.start_head == up._head()
    assert "local change" in up.last_result


def test_check_once_pulls_and_restarts(tmp_path):
    import subprocess

    from core.ops.updater import AutoUpdater, _git

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
    _write(work, "trader/server.py", "1")
    git(work, "add", "-A")
    git(work, "commit", "-m", "initial")
    git(work, "push", "-u", "origin", "HEAD")

    calls = []
    up = AutoUpdater(_cfg(interval=600, enabled=True), str(work), "",
                     restart=lambda *a, **k: calls.append(1))
    assert up.check_once() is False
    assert calls == []

    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    _write(other, "trader/server.py", "2")
    git(other, "add", "-A")
    git(other, "commit", "-m", "second")
    git(other, "push")

    assert up.check_once() is True, up.last_result
    assert calls == [1]
    assert (work / "trader" / "server.py").read_text() == "2"
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


def test_seed_record_from_reflog(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        capture_output=True, check=True,
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(origin), str(work)],
        capture_output=True, check=True,
    )
    _write(work, "trader/server.py", "1")
    subprocess.run(
        ["git", "add", "-A"], cwd=work, env=env,
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "a"], cwd=work, env=env,
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "HEAD"], cwd=work, env=env,
        capture_output=True, check=True,
    )

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(origin), str(other)],
        capture_output=True, check=True,
    )
    _write(other, "trader/server.py", "2")
    subprocess.run(
        ["git", "add", "-A"], cwd=other, env=env,
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "b"], cwd=other, env=env,
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "push"], cwd=other, env=env, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "pull"], cwd=work, env=env, capture_output=True, check=True,
    )

    up = AutoUpdater(_cfg(), str(work), "")
    seeded = up.last_pull()
    assert seeded is not None
    assert seeded["how"] == "pull"
    assert seeded["commit"]
    assert seeded["ts"] > 0


def test_seed_skipped_without_pull_reflog(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    _commit(repo, "a")
    up = AutoUpdater(_cfg(), str(repo), "")
    assert up.last_pull() is None


def test_interval_change_applies_mid_cycle(tmp_path, monkeypatch):
    # a shorter interval saved while a long cycle is waiting must
    # shorten the running wait instead of waiting out the old deadline
    import core.ops.updater as upd

    class FakeClock:
        def __init__(self):
            self.now = 1000.0

        def time(self):
            return self.now

        def sleep(self, secs):
            self.now += secs

    clock = FakeClock()
    monkeypatch.setattr(upd, "time", clock)
    monkeypatch.setattr(upd.AutoUpdater, "_local_head_changed",
                        lambda self: False)

    class Cfg:
        class auto_update:
            enabled = True
            interval_seconds = 600

    class Gate(BaseException):
        pass

    checks = []

    def fake_check(self):
        checks.append(clock.now)
        if len(checks) == 2:
            raise Gate

    monkeypatch.setattr(upd.AutoUpdater, "check_once", fake_check)

    def flip_interval():
        # user saves 60s at t=1100, mid-way through the 600s cycle
        Cfg.auto_update.interval_seconds = 60

    real_sleep = clock.sleep

    def sleep_with_flip(secs):
        if clock.now < 1100 <= clock.now + secs:
            flip_interval()
        real_sleep(secs)

    clock.sleep = sleep_with_flip

    u = upd.AutoUpdater(
        Cfg(), str(tmp_path), ""
    )
    try:
        u._run()
    except Gate:
        pass

    # first check fires ~60s after the mid-cycle change (1100+60),
    # not at the original 600s deadline (1600)
    assert checks[0] < 1250, checks
    # second check is a full 60s later
    assert 55 <= checks[1] - checks[0] <= 75, checks


def test_updater_ignores_untracked_files(tmp_path):
    import subprocess
    from core.ops.updater import AutoUpdater

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True,
                   check=True)
    _write(repo, "trader/server.py", "1")
    _git_run(repo, "add", "-A")
    _commit(repo, "a")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)],
                   capture_output=True, check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)],
                   capture_output=True, check=True)
    _write(work, "trader/server.py", "1")
    _git_run(work, "add", "-A")
    _commit(work, "initial")
    _git_run(work, "push", "-u", "origin", "HEAD")

    # an untracked log file must not block the pull
    (work / "pipeline.log").write_text("log line")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(origin), str(other)],
                   capture_output=True, check=True)
    _write(other, "trader/server.py", "2")
    _git_run(other, "add", "-A")
    _commit(other, "second")
    _git_run(other, "push")

    calls = []
    up = AutoUpdater(_cfg(interval=600, enabled=True), str(work), "",
                     restart=lambda *a, **k: calls.append(1))
    assert up.check_once() is True, up.last_result
    assert calls == [1]


def test_dirty_worktree_matching_origin_converges(tmp_path):
    # a shared checkout: the consumer's release swap writes the
    # exact release content, which looks dirty to git - the info
    # server's updater must converge HEAD instead of skipping
    # the pull forever
    import subprocess

    from core.ops.updater import AutoUpdater, _git

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
    _write(work, "core/x.py", "v1")
    git(work, "add", "-A")
    git(work, "commit", "-m", "v1")
    git(work, "push", "-u", "origin", "HEAD")

    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other))
    _write(other, "core/x.py", "v2")
    git(other, "add", "-A")
    git(other, "commit", "-m", "v2")
    git(other, "push")

    # the release swap already wrote v2 content - no pull yet
    _write(work, "core/x.py", "v2")

    calls = []
    up = AutoUpdater(
        _cfg(interval=600, enabled=True), str(work), "",
        restart=lambda *a, **k: calls.append(1),
    )
    assert up.check_once() is True, up.last_result
    assert calls == [1]
    assert (work / "core" / "x.py").read_text() == "v2"
    head = _git(str(work), "rev-parse", "HEAD").stdout.strip()
    remote = _git(str(other), "rev-parse", "HEAD").stdout.strip()
    assert head == remote
    # the tree is clean afterwards - the next pull is unblocked
    status = _git(str(work), "status", "--porcelain")
    assert not [
        line for line in status.stdout.splitlines()
        if line.strip() and not line.startswith("??")
    ]


def test_dirty_worktree_diverging_still_skips(tmp_path):
    # dirt that does NOT match the remote must still block the
    # pull (real local edits are never clobbered)
    import subprocess

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        capture_output=True, check=True,
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", str(origin), str(work)],
        capture_output=True, check=True,
    )
    _write(work, "core/x.py", "v1")
    _git_run(work, "add", "-A")
    _commit(work, "v1")
    _git_run(work, "push", "-u", "origin", "HEAD")

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(origin), str(other)],
        capture_output=True, check=True,
    )
    _write(other, "core/x.py", "v2")
    _git_run(other, "add", "-A")
    _commit(other, "v2")
    _git_run(other, "push")

    # a real local edit (differs from the remote)
    _write(work, "core/x.py", "local edit")

    calls = []
    up = AutoUpdater(
        _cfg(interval=600, enabled=True), str(work), "",
        restart=lambda *a, **k: calls.append(1),
    )
    assert up.check_once() is False
    assert calls == []
    assert "dirty" in up.last_result
    assert (work / "core" / "x.py").read_text() == "local edit"
