"""Regression tests for the Windows venv launcher bug.

On Windows a venv python.exe is a launcher that spawns the real
interpreter as a child with an IDENTICAL command line. The
stale-process cleanup matched that launcher (same cmdline, same
cwd, different pid) and killed it - exit code 15 - which made
the .bat loop restart the process while the orphaned
interpreter kept running. The cleanup must spare its own
ancestor chain.
"""

import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class _FakeProcess:
    def __init__(self, pid, ppid, cmdline, cwd, name="python.exe"):
        self.pid = pid
        self._ppid = ppid
        self._cwd = cwd
        self.info = {"pid": pid, "name": name, "cmdline": cmdline}
        self.terminated = False

    def ppid(self):
        return self._ppid

    def cwd(self):
        return self._cwd

    def terminate(self):
        self.terminated = True


def _fake_psutil_module(procs, by_pid):
    mod = types.ModuleType("psutil")
    mod.AccessDenied = type("AccessDenied", (Exception,), {})
    mod.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    mod.process_iter = lambda attrs=None: iter(procs)
    mod.Process = lambda pid: by_pid[pid]
    mod.wait_procs = lambda stale, timeout=None: (list(stale), [])
    return mod


def test_protected_pids_walks_ancestors(monkeypatch):
    shell = _FakeProcess(40, 40, [], "x", name="cmd.exe")
    launcher = _FakeProcess(50, 40, [], "x")
    me = _FakeProcess(100, 50, [], "x")
    by_pid = {100: me, 50: launcher, 40: shell}
    fake = _fake_psutil_module([me, launcher, shell], by_pid)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    from trader.ops.processes import _protected_pids

    assert _protected_pids(100) == {100, 50, 40}


def test_stale_cleanup_spares_own_venv_launcher(monkeypatch, tmp_path):
    root = str(tmp_path)
    script = os.path.join(root, "run.py")
    cmd = ["C:\\venv\\Scripts\\python.exe", "run.py"]

    shell = _FakeProcess(40, 40, ["cmd.exe"], "C:\\", name="cmd.exe")
    launcher = _FakeProcess(50, 40, cmd, root)
    me = _FakeProcess(100, 50, cmd, root)
    stranger = _FakeProcess(200, 190, cmd, root)
    unrelated = _FakeProcess(
        300, 290, ["python.exe", "other.py"], root
    )

    by_pid = {
        p.pid: p for p in
        [shell, launcher, me, stranger, unrelated]
    }
    fake = _fake_psutil_module(
        [shell, launcher, me, stranger, unrelated], by_pid
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(os, "getpid", lambda: 100)

    from trader.ops.processes import terminate_stale_instances

    killed = terminate_stale_instances(script)

    # the leftover instance dies, our own launcher does not
    assert killed == [200]
    assert stranger.terminated
    assert not launcher.terminated
    assert not unrelated.terminated


def test_reader_stale_cleanup_spares_own_launcher(monkeypatch):
    reader_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "reader")
    )
    sys.path.insert(0, reader_dir)
    sys.modules.setdefault("uiautomation", MagicMock())
    sys.modules.setdefault("inspect_discord", MagicMock())

    import discord_reader as dr

    cmd = ["C:\\reader.venv\\Scripts\\python.exe", "discord_reader.py"]
    shell = _FakeProcess(40, 40, ["cmd.exe"], "C:\\", name="cmd.exe")
    launcher = _FakeProcess(50, 40, cmd, reader_dir)
    me = _FakeProcess(100, 50, cmd, reader_dir)
    stranger = _FakeProcess(200, 190, cmd, reader_dir)

    by_pid = {
        p.pid: p for p in [shell, launcher, me, stranger]
    }
    fake = _fake_psutil_module(
        [shell, launcher, me, stranger], by_pid
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(os, "getpid", lambda: 100)

    dr._terminate_stale_reader()

    assert stranger.terminated
    assert not launcher.terminated


def test_role_split_instances_do_not_kill_each_other(
    monkeypatch, tmp_path
):
    """The info server and the consumer app share run.py - the
    stale-instance cleanup must scope to the same resolved
    config or the two roles terminate each other in a restart
    loop (seen live: exit code 15 ping-pong between the two
    start.bat loops). Each role runs from its own folder with
    its own config.yaml."""
    root = str(tmp_path)
    script = os.path.join(root, "run.py")
    info_dir = os.path.join(root, "info")
    consumer_dir = os.path.join(root, "consumer")
    info_cmd = [
        "C:\\venv\\Scripts\\python.exe", "run.py",
        "-c", "config.yaml",
    ]
    consumer_cmd = [
        "C:\\venv\\Scripts\\python.exe", "run.py",
        "-c", "config.yaml",
    ]

    info_proc = _FakeProcess(200, 190, info_cmd, info_dir)
    consumer_proc = _FakeProcess(300, 290, consumer_cmd, consumer_dir)
    by_pid = {200: info_proc, 300: consumer_proc}
    fake = _fake_psutil_module([info_proc, consumer_proc], by_pid)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(os, "getpid", lambda: 100)

    from trader.ops.processes import terminate_stale_instances

    # the consumer starts (cwd = consumer/, -c config.yaml):
    # the info server resolves to a different config and must
    # survive; a stale copy of the consumer's own config still
    # dies
    killed = terminate_stale_instances(
        script,
        config_path=os.path.join(consumer_dir, "config.yaml"),
    )
    assert killed == [300]
    assert not info_proc.terminated
    assert consumer_proc.terminated


def test_role_cleanup_matches_default_config_candidates(
    monkeypatch, tmp_path
):
    """A candidate started without -c runs config.yaml in its own
    cwd - it is a different instance from a role-folder start."""
    root = str(tmp_path)
    script = os.path.join(root, "run.py")
    bare_cmd = ["C:\\venv\\Scripts\\python.exe", "run.py"]
    bare = _FakeProcess(200, 190, bare_cmd, root)
    fake = _fake_psutil_module([bare], {200: bare})
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(os, "getpid", lambda: 100)

    from trader.ops.processes import terminate_stale_instances

    killed = terminate_stale_instances(
        script,
        config_path=os.path.join(root, "consumer", "config.yaml"),
    )
    assert killed == []
    assert not bare.terminated

    # ...but a root-config start claims root-config stale copies
    killed = terminate_stale_instances(
        script, config_path=os.path.join(root, "config.yaml")
    )
    assert killed == [200]
    assert bare.terminated
