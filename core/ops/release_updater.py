"""Release-install updater: the client zip shipped through
GitHub Releases.

A release install has no .git - it ships as a zip asset on a
rolling release (default tag consumer-latest, rebuilt on every
push to main). The updater polls the release, downloads a newer
asset, verifies its sha256 and stages the swap; the actual file
swap happens after the process exits (scripts/apply_update.py,
called by the launcher before relaunching) because a live
process holds open handles (sqlite wal, log files) on Windows.
"""

import hashlib
import json
import os
import shutil
import time
import zipfile

from core.redact import format_error, short_error

import requests

from core.ops.remotes import repo_slug_from_url  # noqa: E402
from core.ops.updater import update_record_path  # noqa: E402

# the consumer's release marker at the repo root - outside the
# code dirs an update swap replaces
VERSION_FILE = "VERSION"
# the consumer's own pending marker + update record - the info
# server shares the checkout on the owner's box and must not
# clobber them
PENDING_FILE = ".update_pending_consumer.json"
STAGING_DIR = ".update_staging"


def read_version(root):
    """The VERSION file the build writes; None when missing or
    malformed (a git install has none). Reads the repo-root
    marker first, then the consumer-folder location an
    intermediate build wrote (backward compatibility)."""
    for path in (
        os.path.join(root, VERSION_FILE),
        os.path.join(root, "consumer", "VERSION"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("commit"):
                return data
        except (OSError, ValueError):
            continue
    return None


def _headers(token):
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def latest_release(repo, token, timeout=30):
    """The latest published release, None when none exists.

    A 404 on a repo that IS reachable otherwise means the token is
    missing (a private repo hides its releases from anonymous
    calls) - raise instead of reporting "no release published
    yet", which sent the user chasing a release that could never
    be seen."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    r = requests.get(url, headers=_headers(token), timeout=timeout)
    if r.status_code == 404 and not token:
        raise requests.RequestException(
            "404 - the repo is private and auto_update.github_token "
            "is not set"
        )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def find_asset(release):
    """The consumer zip asset: consumer-<commit>.zip."""
    for a in release.get("assets") or []:
        name = a.get("name") or ""
        if name.startswith("consumer-") and name.endswith(".zip"):
            return a
    return None


def asset_commit(asset):
    name = asset.get("name") or ""
    inner = name[len("consumer-"):-len(".zip")]
    return inner or None


def find_checksum_asset(release):
    for a in release.get("assets") or []:
        if (a.get("name") or "") == "SHA256SUMS":
            return a
    return None


def parse_checksums(text):
    out = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip().lstrip("*")] = parts[0].strip().lower()
    return out


def download_file(url, token, dest, timeout=300):
    h = _headers(token)
    h["Accept"] = "application/octet-stream"
    with requests.get(
        url, headers=h, stream=True, timeout=timeout
    ) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ReleaseUpdater:
    """Polls GitHub Releases and stages a swap for the launcher.

    Same surface as AutoUpdater (start / check_once / last_pull /
    last_check / last_result / errors) so the dashboards and the
    app wiring need no special cases."""

    def __init__(self, cfg, root, webhook_url="", restart=None,
                 restart_files=None):
        self.cfg = cfg
        self.root = root
        self.webhook_url = webhook_url
        self.restart_files = restart_files or ()
        self._restart = restart or (lambda: os._exit(77))
        self._thread = None
        self.last_check = None
        self.last_result = "not checked yet"
        self.errors = 0
        self.version = read_version(root) or {}
        # a VERSION marker inside consumer/ was written by an
        # intermediate build - the marker belongs at the repo root
        # (outside the swapped dirs). move it back, or remove a
        # duplicate when the root already has one
        legacy = os.path.join(root, VERSION_FILE)
        stray = os.path.join(root, "consumer", "VERSION")
        if os.path.exists(stray):
            try:
                if os.path.exists(legacy):
                    os.remove(stray)
                    print("auto-update: removed the duplicate "
                          "consumer/VERSION marker")
                else:
                    shutil.move(stray, legacy)
                    print("auto-update: moved the release marker to "
                          "the repo root")
            except OSError:
                pass
        self.start_head = str(self.version.get("commit") or "")
        self.branch = "release"

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            from core.ops.supervise import supervised

            self._thread, _ = supervised(
                "auto-update", self._run, self.webhook_url
            )

    def last_pull(self):
        try:
            with open(update_record_path(self.root, "consumer"),
                      encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("ts"):
                return data
        except (OSError, ValueError):
            pass
        return None

    def _run(self):
        while True:
            time.sleep(max(30, int(self.cfg.auto_update.interval_seconds)))
            try:
                self.check_once()
            except Exception as e:
                self.errors += 1
                # the git badge renders one line - type + message
                # there, the traceback in the log
                self.last_result = f"error: {short_error(e)}"
                print(f"auto-update error: {format_error(e)}")

    def _token(self):
        return (
            getattr(self.cfg.auto_update, "github_token", "")
            or os.environ.get("GITHUB_TOKEN")
            or ""
        )

    def _seed_version_from_git(self):
        """A git-install consumer switching to the release
        channel: write the VERSION marker from the checkout's
        head + origin so the release comparison starts from the
        code actually running. Best effort - a checkout without
        a GitHub origin cannot follow releases. Called on every
        check for git checkouts: the shared checkout moves with
        the info server's pulls, so the comparison stays anchored
        to the actual head instead of a stale seed."""
        from core.ops.updater import _git

        head = _git(self.root, "rev-parse", "HEAD")
        if head.returncode != 0:
            return False
        commit = head.stdout.strip()[:8]
        remote = _git(self.root, "remote", "get-url", "origin")
        url = remote.stdout.strip() if remote.returncode == 0 else ""
        slug = repo_slug_from_url(url)
        if not slug:
            return False
        if (
            (self.version or {}).get("commit") == commit
            and (self.version or {}).get("repo") == slug
        ):
            return True   # already current - no rewrite
        ver = {
            "tag": "consumer-latest",
            "commit": commit,
            "built": "",
            "repo": slug,
        }
        try:
            with open(os.path.join(self.root, VERSION_FILE), "w",
                      encoding="utf-8") as f:
                json.dump(ver, f, indent=2)
        except OSError:
            return False
        self.version = ver
        self.start_head = str(ver.get("commit") or "")
        print(
            "auto-update: release comparison anchored to the git "
            f"checkout ({commit})"
        )
        return True

    def check_once(self) -> bool:
        self.last_check = time.time()
        if not self.cfg.auto_update.enabled:
            self.last_result = "disabled"
            return False

        if os.path.exists(os.path.join(self.root, PENDING_FILE)):
            # a staged update waits for the launcher to apply it -
            # restarting again would not help
            self.last_result = "staged update pending restart"
            return False

        if os.path.isdir(os.path.join(self.root, ".git")):
            # a shared checkout: the code on disk moves with the
            # info server's pulls - anchor the release comparison
            # to the actual head, not a stale seed
            self._seed_version_from_git()
        local = str(self.version.get("commit") or "")
        repo = str(self.version.get("repo") or "")
        if not repo:
            self.last_result = (
                "no VERSION file and no git origin - cannot "
                "follow releases"
            )
            return False

        try:
            release = latest_release(repo, self._token())
        except requests.RequestException as e:
            self.errors += 1
            self.last_result = f"release check failed: {e}"[:120]
            print(f"auto-update: {self.last_result}")
            return False
        if release is None:
            self.last_result = "no release published yet"
            return False

        asset = find_asset(release)
        if asset is None:
            self.last_result = "release has no consumer zip"
            return False
        remote = asset_commit(asset) or ""
        if not remote or remote == local:
            self.last_result = "up to date"
            return False
        # a git checkout: a release OLDER than the checkout head is
        # a downgrade - the checkout is authoritative (the user
        # pulled newer code; CI's builds lag the pushes). applying
        # an older build would wipe it. release installs (no git)
        # skip this check - their release is always the newest
        from core.ops.updater import _git as _git_cmd

        r = _git_cmd(self.root, "merge-base", "--is-ancestor",
                     remote, "HEAD")
        if r.returncode == 0:
            self.last_result = (
                f"release {remote[:8]} is older than the checkout "
                "head - skipped"
            )
            print(f"auto-update: {self.last_result}")
            return False

        return self._stage(release, asset, remote)

    def _stage(self, release, asset, remote) -> bool:
        staging = os.path.join(self.root, STAGING_DIR)
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)

        token = self._token()
        zip_path = os.path.join(staging, "download.zip")
        try:
            download_file(asset["url"], token, zip_path)
        except requests.RequestException as e:
            self.errors += 1
            self.last_result = f"download failed: {e}"[:120]
            print(f"auto-update: {self.last_result}")
            shutil.rmtree(staging, ignore_errors=True)
            return False

        checksum_asset = find_checksum_asset(release)
        if checksum_asset is None:
            # the build always uploads SHA256SUMS - a release
            # without one is truncated or tampered, and this zip
            # replaces the bot's own code: never install it
            self.errors += 1
            self.last_result = (
                "release has no SHA256SUMS asset - not installing"
            )
            print(f"auto-update: {self.last_result}")
            shutil.rmtree(staging, ignore_errors=True)
            return False
        sums_path = os.path.join(staging, "SHA256SUMS")
        try:
            download_file(checksum_asset["url"], token, sums_path)
            with open(sums_path, encoding="utf-8") as f:
                sums = parse_checksums(f.read())
        except requests.RequestException as e:
            self.errors += 1
            self.last_result = f"checksum download failed: {e}"[:120]
            print(f"auto-update: {self.last_result}")
            shutil.rmtree(staging, ignore_errors=True)
            return False
        expected = sums.get(asset["name"])
        if not expected:
            # a sums file that does not list this asset means
            # the release is not what we expected (renamed
            # artifact, partial upload) - installing an
            # unverified zip is not an option
            self.errors += 1
            self.last_result = (
                f"checksum missing for {asset['name']} - "
                "the SHA256SUMS file does not list it"
            )
            print(f"auto-update: {self.last_result} - not staging")
            shutil.rmtree(staging, ignore_errors=True)
            return False
        if expected != sha256_file(zip_path):
            self.errors += 1
            self.last_result = f"checksum mismatch for {remote}"
            print(f"auto-update: {self.last_result} - not staging")
            shutil.rmtree(staging, ignore_errors=True)
            return False

        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(staging)
        except (OSError, zipfile.BadZipFile) as e:
            self.errors += 1
            self.last_result = f"extract failed: {e}"[:120]
            print(f"auto-update: {self.last_result}")
            shutil.rmtree(staging, ignore_errors=True)
            return False
        os.remove(zip_path)

        # a truncated or wrong artifact must not replace the tree
        if not os.path.isdir(os.path.join(staging, "core")) or not (
            os.path.isdir(os.path.join(staging, "consumer"))
        ):
            self.errors += 1
            self.last_result = "staged build incomplete - aborted"
            print(f"auto-update: {self.last_result}")
            shutil.rmtree(staging, ignore_errors=True)
            return False

        from core.ops.updater import atomic_write_json

        atomic_write_json(
            os.path.join(self.root, PENDING_FILE),
            {
                "staging": STAGING_DIR,
                "commit": remote,
                "ts": time.time(),
            },
        )

        from core.ops.notify import notify_discord

        # the app's restart callable posts the "Consumer app
        # restarting" notice with this reason - one notice, not two
        print(f"auto-update: staged {remote} - restarting to apply...")
        self.last_result = f"staged {remote}"
        self._restart(
            f"release {remote} staged - the launcher applies it "
            "on restart"
        )
        return True