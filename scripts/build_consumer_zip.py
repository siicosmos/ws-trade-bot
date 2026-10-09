"""Build the consumer client zip shipped through GitHub Releases.

The artifact contains everything a release install needs to run:
core/, consumer/ (code only - config, tokens and the db are the
user's), scripts/, run.py, requirements.txt,
config/consumer.example.config.yaml and a VERSION marker the client
updater compares against.

usage: python scripts/build_consumer_zip.py [output_dir]
"""

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# consumer/ files that belong to the user, never to the artifact
CONSUMER_EXCLUDE = (
    "consumer.config.yaml", "ws_tokens.env",
    "pipeline_exit.txt",
    "consumer.trades.db", "consumer.trades.db-shm",
    "consumer.trades.db-wal",
)
EXCLUDE_DIRS = {"__pycache__", ".update_staging", ".update_backup"}
EXCLUDE_EXT = (".pyc", ".pyo")
# the build script itself has no business on the client
SCRIPTS_EXCLUDE = ("build_consumer_zip.py",)


def _git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        timeout=30,
    )


def short_sha():
    sha = os.environ.get("GITHUB_SHA", "")
    if sha:
        return sha[:8]
    r = _git("rev-parse", "HEAD")
    return r.stdout.strip()[:8] if r.returncode == 0 else "unknown"


def repo_slug():
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if slug:
        return slug
    r = _git("remote", "get-url", "origin")
    url = r.stdout.strip() if r.returncode == 0 else ""
    # git@github.com:owner/repo.git or https://.../owner/repo.git
    if ":" in url:
        url = url.split(":", 1)[1]
    elif "github.com/" in url:
        url = url.split("github.com/", 1)[1]
    return url.removesuffix(".git")


def include_file(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return False
    if rel.endswith(EXCLUDE_EXT):
        return False
    if rel.startswith("consumer/"):
        name = parts[-1]
        if name in CONSUMER_EXCLUDE:
            return False
    if rel.startswith("scripts/"):
        name = parts[-1]
        if name in SCRIPTS_EXCLUDE:
            return False
    return True


def collect():
    files = []
    for top in ("core", "consumer", "scripts"):
        base = os.path.join(ROOT, top)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
                if include_file(rel):
                    files.append((full, rel))
    for name in ("run.py", "requirements.txt",
                 os.path.join("config", "consumer.example.config.yaml")):
        full = os.path.join(ROOT, name)
        if os.path.exists(full):
            rel = name.replace(os.sep, "/")
            files.append((full, rel))
    return files


def build(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    commit = short_sha()
    slug = repo_slug()
    version = {
        "tag": "consumer-latest",
        "commit": commit,
        "built": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "repo": slug,
    }
    zip_path = os.path.join(out_dir, f"consumer-{commit}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for full, rel in collect():
            z.write(full, rel)
        z.writestr("VERSION", json.dumps(version, indent=2) + "\n")

    digest = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    sums_path = os.path.join(out_dir, "SHA256SUMS")
    with open(sums_path, "w", encoding="utf-8") as f:
        f.write(f"{digest.hexdigest()}  {os.path.basename(zip_path)}\n")
    print(f"built {zip_path}")
    print(f"built {sums_path}")
    return zip_path, sums_path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "dist"
    )
    build(out)