"""The consumer release artifact: everything a client install
needs, nothing it must not get."""

import hashlib
import importlib.util
import json
import os
import re
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_build():
    spec = importlib.util.spec_from_file_location(
        "build_consumer_zip",
        os.path.join(ROOT, "scripts", "build_consumer_zip.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_zip_artifact(tmp_path):
    mod = _load_build()
    out = str(tmp_path / "dist")
    zip_path, sums_path = mod.build(out)

    # naming: consumer-<8 char commit>.zip
    name = os.path.basename(zip_path)
    assert name.startswith("consumer-") and name.endswith(".zip")
    commit = name[len("consumer-"):-len(".zip")]
    assert len(commit) == 8

    # SHA256SUMS matches the actual zip bytes
    digest = hashlib.sha256(open(zip_path, "rb").read()).hexdigest()
    assert open(sums_path, encoding="utf-8").read() == f"{digest}  {name}\n"

    z = zipfile.ZipFile(zip_path)
    names = z.namelist()

    # everything a client install needs
    required = [
        "run.py", "requirements.txt", "VERSION",
        "config/consumer.config.yaml",
        "scripts/install_consumer.bat",
        "scripts/start_consumer.bat",
        "scripts/apply_update.py",
        "scripts/ws_login.py",
        "scripts/gen_cert.py",
        "core/config.py",
        "core/ops/updater.py",
        "core/ops/release_updater.py",
        "consumer/app.py",
        "consumer/web.py",
        "consumer/static/dashboard.js",
    ]
    for n in required:
        assert n in names, f"missing from artifact: {n}"

    # nothing that belongs to the user or the build machine
    forbidden_fragments = (
        "ws_tokens.env", ".db", "__pycache__", ".pyc",
        "config.example.yaml", ".update_staging", ".update_backup",
    )
    leaks = [
        n for n in names
        if any(f in n for f in forbidden_fragments)
    ]
    assert leaks == [], leaks

    # the VERSION marker drives the client's update comparison
    ver = json.loads(z.read("VERSION"))
    assert ver["commit"] == commit
    assert ver["repo"] == "siicosmos/ws-trade-bot"
    assert ver["tag"] == "consumer-latest"
    assert ver["built"]

    # the shipped template is the aligned one: blank accounts,
    # the section key is the role, and no token ships in it
    template = z.read("config/consumer.config.yaml").decode()
    assert "accounts: []" in template
    assert re.search(r"^consumer:", template, re.M)
    assert not re.search(r"^pipeline:", template, re.M)
    for line in template.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("github_token:"):
            raise AssertionError(f"token shipped in the template: {line}")