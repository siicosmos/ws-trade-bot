import importlib.util
import json
import os
import zipfile

import pytest

import core.ops.release_updater as ru
from core.ops.release_updater import ReleaseUpdater, read_version
from core.ops.updater import AutoUpdater, create_updater


def _cfg(enabled=True, token="", role="consumer"):
    return type(
        "C", (), {
            "auto_update": type(
                "A", (), {
                    "interval_seconds": 600, "enabled": enabled,
                    "github_token": token,
                    "release_tag": "consumer-latest",
                }
            )(),
            "pipeline": type("P", (), {"role": role})(),
        },
    )()


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _release(commit):
    return {
        "tag_name": "consumer-latest",
        "assets": [
            {"name": f"consumer-{commit}.zip",
             "url": f"https://fake/{commit}.zip"},
            {"name": "SHA256SUMS", "url": "https://fake/sums"},
        ],
    }


def _make_zip(path, commit):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("core/mod.py", "x = 1\n")
        z.writestr("consumer/web.py", "y = 2\n")
        z.writestr("consumer/static/app.js", "// js\n")
        z.writestr("run.py", "print('hi')\n")
        z.writestr(
            "consumer/VERSION",
            json.dumps({"commit": commit, "repo": "o/r"}) + "\n",
        )


def _load_apply_update():
    spec = importlib.util.spec_from_file_location(
        "apply_update",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "apply_update.py",
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_read_version_roundtrip(tmp_path):
    assert read_version(str(tmp_path)) is None
    _write(tmp_path, "consumer/VERSION", json.dumps(
        {"commit": "aaa", "repo": "o/r"}))
    ver = read_version(str(tmp_path))
    assert ver["commit"] == "aaa"
    assert ver["repo"] == "o/r"
    _write(tmp_path, "consumer/VERSION", "not json")
    assert read_version(str(tmp_path)) is None


def test_find_asset_and_commit():
    release = _release("bbb")
    asset = ru.find_asset(release)
    assert ru.asset_commit(asset) == "bbb"
    assert ru.find_asset({"assets": [{"name": "other.zip"}]}) is None
    assert ru.parse_checksums(
        "abc123  consumer-bbb.zip\nxyz  *other.zip\n") == {
        "consumer-bbb.zip": "abc123", "other.zip": "xyz",
    }


def test_check_once_up_to_date(tmp_path, monkeypatch):
    _write(tmp_path, "consumer/VERSION", json.dumps(
        {"commit": "aaa", "repo": "o/r"}))
    monkeypatch.setattr(ru, "latest_release", lambda *a, **k: _release("aaa"))
    up = ReleaseUpdater(_cfg(), str(tmp_path), "")
    assert up.check_once() is False
    assert up.last_result == "up to date"


def test_check_once_stages_and_restarts(tmp_path, monkeypatch):
    _write(tmp_path, "consumer/VERSION", json.dumps(
        {"commit": "aaa", "repo": "o/r"}))
    release = _release("bbb")

    # build the zip once so the sums can match it
    zip_bytes = {}

    def download(url, token, dest, timeout=300):
        if url.endswith(".zip"):
            _make_zip(dest, "bbb")
            with open(dest, "rb") as f:
                zip_bytes["data"] = f.read()
        else:
            import hashlib
            digest = hashlib.sha256(zip_bytes["data"]).hexdigest()
            with open(dest, "w", encoding="utf-8") as f:
                f.write(f"{digest}  consumer-bbb.zip\n")

    monkeypatch.setattr(ru, "latest_release", lambda *a, **k: release)
    monkeypatch.setattr(ru, "download_file", download)

    calls = []
    up = ReleaseUpdater(
        _cfg(), str(tmp_path), "", restart=lambda *a, **k: calls.append(1)
    )
    assert up.check_once() is True
    assert calls == [1]
    assert up.last_result == "staged bbb"
    # the swap has NOT happened yet - the launcher applies it
    assert read_version(str(tmp_path))["commit"] == "aaa"
    staging = os.path.join(str(tmp_path), ".update_staging")
    assert os.path.isfile(os.path.join(staging, "core", "mod.py"))
    assert os.path.isfile(os.path.join(staging, "consumer", "web.py"))
    assert os.path.isfile(os.path.join(str(tmp_path),
                                       ru.PENDING_FILE))
    # the downloaded zip is not left in the staging tree
    assert not os.path.exists(os.path.join(staging, "download.zip"))


def test_checksum_mismatch_aborts(tmp_path, monkeypatch):
    _write(tmp_path, "consumer/VERSION", json.dumps(
        {"commit": "aaa", "repo": "o/r"}))
    release = _release("bbb")

    def download(url, token, dest, timeout=300):
        if url.endswith(".zip"):
            _make_zip(dest, "bbb")
        else:
            with open(dest, "w", encoding="utf-8") as f:
                f.write("deadbeef  consumer-bbb.zip\n")

    monkeypatch.setattr(ru, "latest_release", lambda *a, **k: release)
    monkeypatch.setattr(ru, "download_file", download)

    calls = []
    up = ReleaseUpdater(
        _cfg(), str(tmp_path), "", restart=lambda *a, **k: calls.append(1)
    )
    assert up.check_once() is False
    assert calls == []
    assert "checksum" in up.last_result
    assert not os.path.exists(os.path.join(str(tmp_path),
                                           ru.PENDING_FILE))
    assert not os.path.exists(os.path.join(str(tmp_path),
                                           ".update_staging"))


def test_check_once_disabled_and_pending(tmp_path, monkeypatch):
    _write(tmp_path, "consumer/VERSION", json.dumps(
        {"commit": "aaa", "repo": "o/r"}))
    up = ReleaseUpdater(_cfg(enabled=False), str(tmp_path), "")
    assert up.check_once() is False
    assert up.last_result == "disabled"

    _write(tmp_path, ru.PENDING_FILE, json.dumps(
        {"staging": ".update_staging", "commit": "bbb", "ts": 1}))
    monkeypatch.setattr(
        ru, "latest_release",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("polled")),
    )
    up2 = ReleaseUpdater(_cfg(), str(tmp_path), "")
    assert up2.check_once() is False
    assert up2.last_result == "staged update pending restart"


def test_apply_update_swaps_and_preserves_state(tmp_path):
    root = str(tmp_path)
    # the old install: state lives in config/ + db/, apart from
    # the code dirs the swap replaces
    _write(root, "config/consumer.config.yaml", "role: consumer\n")
    _write(root, "config/ws_tokens.env", "T=1\n")
    _write(root, "db/consumer.trades.db", "db-bytes")
    _write(root, "core/old.py", "old\n")
    _write(root, "run.py", "old run\n")
    # the staged build
    staging = os.path.join(root, ".update_staging")
    os.makedirs(staging)
    _make_zip(os.path.join(staging, "x.zip"), "bbb")
    with zipfile.ZipFile(os.path.join(staging, "x.zip")) as z:
        z.extractall(staging)
    os.remove(os.path.join(staging, "x.zip"))
    _write(staging, "scripts/apply_update.py", "# new\n")
    _write(staging, "scripts/start_consumer.bat", "@echo off\n")
    _write(root, ru.PENDING_FILE, json.dumps(
        {"staging": ".update_staging", "commit": "bbb", "ts": 1}))

    mod = _load_apply_update()
    assert mod.apply(root) is True

    # code swapped
    with open(os.path.join(root, "core", "mod.py"), encoding="utf-8") as f:
        assert f.read() == "x = 1\n"
    assert not os.path.exists(os.path.join(root, "core", "old.py"))
    with open(os.path.join(root, "run.py"), encoding="utf-8") as f:
        assert f.read() == "print('hi')\n"
    # state untouched - the swap never touches config/ or db/
    with open(os.path.join(root, "config", "consumer.config.yaml"), encoding="utf-8") as f:
        assert f.read() == "role: consumer\n"
    with open(os.path.join(root, "db", "consumer.trades.db"), encoding="utf-8") as f:
        assert f.read() == "db-bytes"
    with open(os.path.join(root, "config", "ws_tokens.env"), encoding="utf-8") as f:
        assert f.read() == "T=1\n"
    # VERSION + record + cleanup
    assert read_version(root)["commit"] == "bbb"
    with open(os.path.join(root, ".last_update_consumer.json"),
              encoding="utf-8") as f:
        rec = json.load(f)
    assert rec["how"] == "release" and rec["commit"] == "bbb"
    assert not os.path.exists(os.path.join(root, ru.PENDING_FILE))
    assert not os.path.exists(staging)
    # .bat files are never swapped (cmd re-reads the running file)
    assert not os.path.exists(os.path.join(root, "scripts",
                                           "start_consumer.bat"))
    assert os.path.isfile(os.path.join(root, "scripts",
                                       "apply_update.py"))


def test_apply_update_noop_without_pending(tmp_path):
    mod = _load_apply_update()
    assert mod.apply(str(tmp_path)) is False


def test_apply_update_failure_leaves_state_untouched(tmp_path, monkeypatch):
    # a swap that dies mid-way may leave the code broken, but the
    # state lives in config/ + db/ which the swap never touches -
    # the data survives no matter where the failure lands
    root = str(tmp_path)
    _write(root, "config/consumer.config.yaml", "role: consumer\n")
    _write(root, "db/consumer.trades.db", "db-bytes")
    _write(root, "core/old.py", "old\n")
    staging = os.path.join(root, ".update_staging")
    os.makedirs(staging)
    _make_zip(os.path.join(staging, "x.zip"), "bbb")
    with zipfile.ZipFile(os.path.join(staging, "x.zip")) as z:
        z.extractall(staging)
    os.remove(os.path.join(staging, "x.zip"))
    _write(root, ru.PENDING_FILE, json.dumps(
        {"staging": ".update_staging", "commit": "bbb", "ts": 1}))

    mod = _load_apply_update()
    real_copytree = mod.shutil.copytree

    def flaky_copytree(src, dst, *a, **k):
        if os.path.basename(os.path.normpath(dst)) == "consumer":
            raise OSError("injected copy failure")
        return real_copytree(src, dst, *a, **k)

    monkeypatch.setattr(mod.shutil, "copytree", flaky_copytree)

    with pytest.raises(OSError):
        mod.apply(root)

    # the state files are untouched
    with open(os.path.join(root, "config", "consumer.config.yaml"), encoding="utf-8") as f:
        assert f.read() == "role: consumer\n"
    with open(os.path.join(root, "db", "consumer.trades.db"), encoding="utf-8") as f:
        assert f.read() == "db-bytes"
    # the marker + staging stay - the next launcher pass retries
    assert os.path.exists(os.path.join(root, ru.PENDING_FILE))
    assert os.path.isdir(staging)


def test_incomplete_staged_build_aborts(tmp_path, monkeypatch):
    # a truncated or wrong artifact must not replace the tree
    _write(tmp_path, "consumer/VERSION", json.dumps(
        {"commit": "aaa", "repo": "o/r"}))
    release = _release("bbb")

    zip_path = {}

    def download(url, token, dest, timeout=300):
        if url.endswith(".zip"):
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr("run.py", "print('hi')\n")   # no core/ consumer/
            zip_path["p"] = dest
        else:
            # the sums list the (bad) zip so the checksum passes
            # and the extract-stage completeness check is what
            # aborts
            import hashlib
            digest = hashlib.sha256(
                open(zip_path["p"], "rb").read()).hexdigest()
            with open(dest, "w", encoding="utf-8") as f:
                f.write(f"{digest}  consumer-bbb.zip\n")

    monkeypatch.setattr(ru, "latest_release", lambda *a, **k: release)
    monkeypatch.setattr(ru, "download_file", download)

    calls = []
    up = ReleaseUpdater(
        _cfg(), str(tmp_path), "", restart=lambda *a, **k: calls.append(1)
    )
    assert up.check_once() is False
    assert calls == []
    assert "incomplete" in up.last_result
    assert not os.path.exists(os.path.join(str(tmp_path),
                                           ru.PENDING_FILE))
    assert not os.path.exists(os.path.join(str(tmp_path),
                                           ".update_staging"))


def test_create_updater_routes_by_role(tmp_path):
    # the consumer follows the release channel even in a git
    # checkout (VERSION is seeded from git on the first check);
    # the info server keeps the git-pull updater
    a = os.path.join(str(tmp_path), "a")
    os.makedirs(os.path.join(a, ".git"))
    assert type(
        create_updater(_cfg(role="consumer"), a, "")
    ) is ReleaseUpdater

    b = os.path.join(str(tmp_path), "b")
    _write(b, "consumer/VERSION", json.dumps({"commit": "aaa", "repo": "o/r"}))
    assert type(
        create_updater(_cfg(role="info"), b, "")
    ) is AutoUpdater


def test_release_updater_seeds_version_from_git(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "consumer").mkdir()   # the marker seeds into consumer/
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "init"], cwd=repo, env=env, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "a"],
        cwd=repo, env=env, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin",
         "git@github.com:siicosmos/ws-trade-bot.git"],
        cwd=repo, env=env, capture_output=True, check=True,
    )

    monkeypatch.setattr(ru, "latest_release", lambda *a, **k: None)
    up = ReleaseUpdater(_cfg(), str(repo), "")
    assert up.check_once() is False
    # the first check seeded VERSION from the checkout instead
    assert read_version(str(repo))["repo"] == "siicosmos/ws-trade-bot"
    assert up.last_result == "no release published yet"

    # the next check polls with the seeded identity
    monkeypatch.setattr(
        ru, "latest_release", lambda *a, **k: _release(
            read_version(str(repo))["commit"])
    )
    assert up.check_once() is False
    assert up.last_result == "up to date"


def test_status_payload_compatible(tmp_path):
    from core.ops.updater import update_status_payload

    b = os.path.join(str(tmp_path), "b")
    _write(b, "consumer/VERSION", json.dumps({"commit": "aaa", "repo": "o/r"}))
    app = type("A", (), {"ws_updater": ReleaseUpdater(_cfg(), b, "")})()
    payload = update_status_payload(app)
    assert payload["status"] == "active"
    assert payload["head"] == "aaa"
    assert payload["branch"] == "release"

def test_checksum_missing_from_sums_aborts(tmp_path, monkeypatch):
    """a SHA256SUMS file that does not list the asset (renamed
    artifact, partial upload) must abort - the old code silently
    skipped the check and staged the unverified zip."""
    _write(tmp_path, "consumer/VERSION", json.dumps(
        {"commit": "aaa", "repo": "o/r"}))
    release = _release("bbb")

    def download(url, token, dest, timeout=300):
        if url.endswith(".zip"):
            _make_zip(dest, "bbb")
        else:
            # sums for a DIFFERENT asset name - the real zip is
            # not listed
            with open(dest, "w", encoding="utf-8") as f:
                f.write("deadbeef  consumer-old.zip\n")

    monkeypatch.setattr(ru, "latest_release", lambda *a, **k: release)
    monkeypatch.setattr(ru, "download_file", download)

    calls = []
    up = ReleaseUpdater(
        _cfg(), str(tmp_path), "", restart=lambda *a, **k: calls.append(1)
    )
    assert up.check_once() is False
    assert calls == []
    assert "checksum missing" in up.last_result
    assert not os.path.exists(os.path.join(
        str(tmp_path), ru.PENDING_FILE))
