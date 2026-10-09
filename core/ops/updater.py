import json
import os
import subprocess
import threading
import time

from core.redact import format_error


# per-role update records: the info server and the consumer app
# share a checkout on the owner's box - separate files so their
# records never overwrite each other
UPDATE_RECORDS = {
    "info": ".last_update_info.json",
    "consumer": ".last_update_consumer.json",
}


def pop_exit_marker(db_path, role):
    """Read + remove the launcher's exit-code marker (written
    next to the db as db/pipeline_exit_<role>.txt); returns the
    previous run's last words ("")."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(db_path)),
        f"pipeline_exit_{role}.txt",
    )
    prev = ""
    try:
        with open(path, encoding="utf-8") as f:
            prev = f.read().strip()
        os.remove(path)
    except OSError:
        pass
    return prev


def update_record_path(root, role):
    name = UPDATE_RECORDS.get(role, UPDATE_RECORDS["consumer"])
    return os.path.join(root, name)


PIPELINE_RESTART_FILES = (
    "core/*", "info/*", "consumer/*",
    "run.py", "requirements.txt",
)
# per-role globs: each app restarts only when code it executes
# changed (the shared core moves both). the reader runs no
# updater - it restarts off the info server's pull
INFO_RESTART_FILES = ("core/*", "info/*", "run.py", "requirements.txt")
CONSUMER_RESTART_FILES = ("core/*", "consumer/*", "run.py",
                          "requirements.txt")


def atomic_write_json(path, data):
    """Crash-safe json state write: temp file + os.replace (with
    the windows permission retry). A torn pending marker
    made apply_update drop a staged update; the update record is
    the banner's update provenance."""
    import tempfile

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    os.unlink(tmp)
                    raise
                time.sleep(0.2 * (attempt + 1))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def git_changed_files(root, old, new):
    """Files changed between two commits, None when unknown."""
    try:
        r = _git(root, "diff", "--name-only", f"{old}..{new}")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def files_match(files, patterns):
    from fnmatch import fnmatch

    return any(fnmatch(f, p) for f in files or [] for p in patterns)


def _git(root, *args):
    # never hang on credential prompts - a headless failure is
    # visible in the logs, a hung one just looks like a dead
    # updater
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        timeout=120, env=env,
    )


def _clear_stale_lock(root):
    """Remove a stale .git/index.lock left by a git process that
    died mid-operation (machine slept, hard power off). Every
    subsequent git call fails until it is gone."""
    lock = os.path.join(root, ".git", "index.lock")
    try:
        age = time.time() - os.path.getmtime(lock)
    except OSError:
        return False
    if age < 1800:
        # possibly live - hands off (the mtime is written once at
        # creation and never refreshed, so a slow fetch on a
        # flapped network stays "young" for its whole run)
        return False
    try:
        os.remove(lock)
        print(
            "auto-update: removed stale .git/index.lock "
            f"(age {int(age)}s)"
        )
        return True
    except OSError:
        return False


RUNTIME_IGNORED = (
    "pipeline.log*", "reader.log*", "info.log*", "consumer.log*",
    "logs/*", "*.db",
    "db/*",
    ".last_update_info.json", ".last_update_consumer.json",
    ".update_pending_consumer.json", ".reader_seen.json",
    ".consumer_session_key", ".code_swapped.json",
    "config/consumer.config.yaml", "config/info.config.yaml",
    "config/reader.config.yaml", "config/ws_tokens.env",
    "*.pyc", "__pycache__/*",
)


def app_name(cfg):
    """Discord title prefix for this process's role: the info
    server and the consumer app share the updater, the reader
    has its own."""
    role = str(
        getattr(getattr(cfg, "pipeline", None), "role", "")
        or "consumer"
    )
    return "Info server" if role == "info" else "Consumer app"


def _is_ignored_runtime_file(root, path):
    """True when a dirty tracked file is an ignored runtime file
    (logs, dbs) - safe to restore since it regenerates anyway."""
    from fnmatch import fnmatch

    if any(fnmatch(path, p) for p in RUNTIME_IGNORED):
        return True
    r = _git(root, "check-ignore", "--", path)
    return r.returncode == 0


def startup_banner(name, root):
    """Startup line for the logs: the commit being run and how it
    got there (auto-update / manual pull)."""
    from core.ops.release_updater import read_version, update_record_path

    role = "info" if name.startswith("info") else "consumer"
    record_path = update_record_path(root, role)
    ver = read_version(root)
    head = _git(root, "rev-parse", "--short", "HEAD")
    git_commit = head.stdout.strip() if head.returncode == 0 else ""
    if ver and not git_commit:
        # release install - no git history to read
        commit = str(ver.get("commit") or "?")
        line = f"{name} starting - release {commit}"
        built = str(ver.get("built") or "")
        if built:
            line += f" (built {built})"
    else:
        # a git checkout tracks the real code on disk - the
        # VERSION marker a release seed wrote goes stale as the
        # shared checkout moves, so git wins when it exists
        commit = git_commit
        subject = _git(root, "log", "-1", "--pretty=%s")
        line = f"{name} starting - commit {commit or '?'}"
        if subject.returncode == 0 and subject.stdout.strip():
            line += f' "{subject.stdout.strip()}"'
    try:
        with open(record_path, encoding="utf-8") as f:
            rec = json.load(f)
        if (
            isinstance(rec, dict)
            and rec.get("commit")
            and rec["commit"] == (commit or "")
        ):
            how = str(rec.get("how") or "pull")
            ts = rec.get("ts") or 0
            age = time.time() - float(ts)
            if age < 0:
                ago = "just now"
            elif age < 3600:
                ago = f"{int(age // 60)}m ago"
            elif age < 86400:
                ago = f"{int(age // 3600)}h ago"
            else:
                ago = f"{int(age // 86400)}d ago"
            line += (
                " (auto-updated " + ago + ")"
                if how == "auto" else
                " (updated " + ago + " via " + how + ")"
            )
    except (OSError, ValueError, TypeError):
        pass
    print(line)
    return line


class AutoUpdater:
    def __init__(self, cfg, root, webhook_url="", restart=None,
                 restart_files=None):
        self.cfg = cfg
        self.root = root
        self.role = str(
            getattr(getattr(cfg, "pipeline", None), "role", "")
            or "consumer"
        )
        self.update_record = update_record_path(root, self.role)
        self.webhook_url = webhook_url
        self.name = app_name(cfg)
        self.restart_files = restart_files or PIPELINE_RESTART_FILES
        self._restart = restart or (lambda: os._exit(77))
        self._thread = None
        self.started = time.time()
        self.last_check = None
        self.last_result = "not checked yet"
        self.errors = 0
        self.start_head = self._head()
        self.branch = ""
        r = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode == 0:
            self.branch = r.stdout.strip()
        self._seed_record_if_missing()
        self._refresh_record_at_startup()

    def _refresh_record_at_startup(self):
        """The record must describe the code actually running: a
        record left at an older commit (a manual git pull, a seed
        from an old reflog entry) made the banner and the
        dashboard's last_pull point at a hash that is not ours."""
        head = self._head()
        if not head:
            return
        rec = self.last_pull() or {}
        if rec.get("commit") == head[:8]:
            return   # already current - keep the original ts
        self._record_update("startup")

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            from core.ops.supervise import supervised

            self._thread, _ = supervised(
                "auto-update", self._run, self.webhook_url
            )

    def _head(self):
        r = _git(self.root, "rev-parse", "HEAD")
        if r.returncode == 0:
            return r.stdout.strip() or None
        return None

    def _code_swapped_after_start(self):
        """True when a release install swapped the code dirs after
        this process started (the marker records the swap time)."""
        path = os.path.join(self.root, ".code_swapped.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return float(data.get("ts") or 0) > self.started
        except (OSError, ValueError, TypeError):
            return False

    def _local_head_changed(self):
        current = self._head()
        return bool(current and self.start_head and current != self.start_head)

    def _record_update(self, how):
        new = self._head()
        try:
            atomic_write_json(
                self.update_record,
                {
                    "how": how,
                    "commit": new[:8] if new else "?",
                    "ts": time.time(),
                },
            )
        except OSError:
            pass

    def last_pull(self):
        try:
            with open(self.update_record, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("ts"):
                return data
        except (OSError, ValueError):
            pass
        return None

    def _seed_record_if_missing(self):
        if self.last_pull() is not None:
            return
        try:
            r = _git(
                self.root, "reflog", "-1",
                "--format=%gs|%ct", "HEAD",
            )
            if r.returncode != 0:
                return
            msg, _, ts = r.stdout.strip().partition("|")
            if "pull" not in msg.lower() or not ts.isdigit():
                return
            new = self._head()
            with open(self.update_record, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "how": "pull",
                        "commit": new[:8] if new else "?",
                        "ts": int(ts),
                    },
                    f,
                )
        except (OSError, ValueError):
            pass

    def _heal_ignored_runtime_files(self, dirty_lines):
        """Restore dirty tracked files that are ignored runtime
        files (logs, dbs) so a pull can proceed - the reader.log
        class of incident, where a runtime file was once
        committed and then locally modified."""
        import re as _re

        healed = []
        for line in dirty_lines:
            # porcelain -z separates entries with NUL and never
            # quotes; a rename arrives as TWO fragments ("XY new"
            # then the bare old path) - fragments without a
            # status prefix are the old-path halves, not entries
            if not _re.match(r"^[A-Z?! ]{2} ", line):
                continue
            path = line[3:].strip()
            if not path or not _is_ignored_runtime_file(
                self.root, path
            ):
                continue
            _git(self.root, "checkout", "--", path)
            status = _git(self.root, "status", "--porcelain",
                          "--", path)
            if status.returncode == 0 and not status.stdout.strip():
                healed.append(path)
            else:
                # still dirty (deleted-from-tracking etc) - drop
                # it from the work tree entirely
                _git(self.root, "clean", "-f", "--", path)
                healed.append(path + " (removed)")
        if healed:
            print(
                "auto-update: restored ignored runtime files: "
                + ", ".join(healed)
            )
        return healed

    def _restart_for_local_change(self):
        """Returns True when a restart was initiated (the process
        exits), False when the change is irrelevant and the
        update loop must keep running."""
        new = self._head()
        from core.ops.notify import notify_discord

        changed = git_changed_files(self.root, self.start_head, new)
        if changed is not None and not files_match(
            changed, getattr(self, "restart_files",
                           PIPELINE_RESTART_FILES)
        ):
            print("auto-update: local change does not touch the "
                  "pipeline - not restarting")
            self.start_head = new
            return False

        self._record_update("manual")

        commit = ""
        try:
            log = _git(self.root, "log", "-1", "--oneline", new or "HEAD")
            commit = log.stdout.strip()
        except (OSError, ValueError):
            pass
        print("auto-update: local code changed - restarting pipeline...")
        self.last_result = f"local change: {new[:8] if new else '?'}"
        self._restart(
            reason=f"local code changed to {new[:8] if new else '?'}",
            commits=commit[:1000] or "-",
        )
        return True

    def _run(self):
        while True:
            interval = max(30, int(self.cfg.auto_update.interval_seconds))
            deadline = time.time() + interval
            while True:
                # a shorter interval saved mid-cycle takes effect now
                # instead of after the old deadline
                interval = max(
                    30, int(self.cfg.auto_update.interval_seconds)
                )
                deadline = min(deadline, time.time() + interval)
                remain = deadline - time.time()
                if remain <= 0:
                    break
                time.sleep(min(15, remain))
                try:
                    if self._code_swapped_after_start():
                        print(
                            "auto-update: the code dirs were swapped "
                            "by a release install - restarting to "
                            "load it"
                        )
                        self._restart()
                        return
                    if self._local_head_changed():
                        # only leave the loop when a restart was
                        # actually initiated; a docs-only local
                        # change must not kill the update thread
                        if self._restart_for_local_change():
                            return
                except Exception as e:
                    # keep the loop alive, but stay visible: a
                    # persistently failing local-change check must
                    # not look like "everything is fine"
                    print(f"auto-update: local-change check failed: "
                          f"{format_error(e)}")
            try:
                self.check_once()
            except Exception as e:
                self.errors += 1
                # the git badge renders one line - type + message
                # there, the traceback in the log
                self.last_result = f"error: {type(e).__name__}: {e}"
                print(f"auto-update error: {format_error(e)}")

    def check_once(self) -> bool:
        self.last_check = time.time()
        if not self.cfg.auto_update.enabled:
            self.last_result = "disabled"
            return False

        r = _git(self.root, "rev-parse", "--is-inside-work-tree")
        if r.returncode != 0:
            self.last_result = "not a git repo"
            return False

        status = _git(
            self.root, "-c", "core.quotepath=false",
            "status", "--porcelain", "-z",
        )
        # untracked files never conflict with a pull (logs, dbs) -
        # only modifications to tracked files block it. -z: NUL-
        # separated, never quoted (renames and non-ascii paths
        # parsed garbage in the line format)
        dirty = [
            line for line in status.stdout.split("\x00")
            if line.strip() and not line.startswith("??")
        ]
        if dirty:
            self._heal_ignored_runtime_files(dirty)
            status = _git(
                self.root, "-c", "core.quotepath=false",
                "status", "--porcelain", "-z",
            )
            dirty = [
                line for line in status.stdout.split("\x00")
                if line.strip() and not line.startswith("??")
            ]
        dirty_names = ", ".join(
            line[3:].strip('"') for line in dirty[:4]
        ) or "?"

        branch = _git(
            self.root, "rev-parse", "--abbrev-ref", "HEAD"
        ).stdout.strip()
        short = branch if branch and branch != "HEAD" else "main"
        ref = f"origin/{short}"

        fetch = _git(self.root, "fetch", "origin")
        if fetch.returncode != 0:
            if "index.lock" in (
                fetch.stderr or ""
            ) and _clear_stale_lock(self.root):
                fetch = _git(self.root, "fetch", "origin")
            if fetch.returncode != 0:
                self.last_result = (
                    "fetch failed: "
                    + (fetch.stderr or "").strip()[:120]
                )
                print(
                    "auto-update: fetch failed: "
                    + (fetch.stderr or "").strip()
                )
                return False

        local = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        remote = _git(self.root, "rev-parse", ref).stdout.strip()
        if not remote or local == remote:
            self.last_result = "up to date"
            return False

        if dirty:
            # a shared checkout: the consumer's release swap
            # writes the exact release content, which makes the
            # tree look dirty to git. when the worktree already
            # matches the remote, converge HEAD instead of
            # skipping the pull forever
            diff = _git(self.root, "diff", "--stat", ref)
            if diff.returncode != 0 or diff.stdout.strip():
                self.last_result = (
                    "skipped: working tree dirty (" + dirty_names + ")"
                )
                print(
                    "auto-update: working tree dirty, skipping pull: "
                    + dirty_names
                )
                return False
            reset = _git(self.root, "reset", "--hard", ref)
            if reset.returncode != 0:
                self.last_result = (
                    "reset failed: "
                    + (reset.stderr or "").strip()[:120]
                )
                print(f"auto-update: {self.last_result}")
                return False
            print(
                "auto-update: worktree already matches the release "
                f"content - converged HEAD to {remote[:8]}"
            )
        else:
            pull = _git(self.root, "pull", "--ff-only", "origin", short)
            if pull.returncode != 0:
                self.last_result = (
                    f"pull failed: {pull.stderr.strip()[:120]}"
                )
                print(f"auto-update: pull failed: {pull.stderr.strip()}")
                return False

        new = _git(self.root, "rev-parse", "HEAD").stdout.strip()
        log = _git(self.root, "log", "--oneline", f"{local}..{new}")
        commits = log.stdout.strip()

        print(f"auto-update: updated to {new[:8]}:\n{commits}")
        self.last_result = f"updated to {new[:8]}"

        from core.ops.notify import notify_discord

        changed = git_changed_files(self.root, local, new)
        if changed is not None and not files_match(
            changed, getattr(self, "restart_files",
                           PIPELINE_RESTART_FILES)
        ):
            # the pull still updates docs/reader/tests - just not
            # anything this process executes
            notify_discord(
                self.webhook_url,
                f"{self.name} updated (no restart)",
                {
                    "reason": f"code updated to {new[:8]} - no "
                               "pipeline files changed",
                    "commits": commits[:1000] or "-",
                },
                ok=True,
            )
            print("auto-update: no pipeline changes - not restarting")
            # keep the loop's baseline current so the next poll
            # does not mistake our own pull for a local change
            self.start_head = new
            # record the move: the code on disk is at `new` now -
            # an unrecorded pull left the update record (and the
            # dashboard's last_pull) pointing at the old commit
            self._record_update("auto")
            return False

        print("auto-update: restarting pipeline...")
        self._record_update("auto")
        # the app's _restart owns the webhook notice (it carries
        # the reason + commits) - posting here too double-posted
        self._restart(
            reason=f"code updated to {new[:8]}",
            commits=commits[:1000] or "-",
        )
        return True


def create_updater(cfg, root, webhook_url="", restart=None,
                   restart_files=None):
    """Role-based updater selection:

    - consumer app (the shipped client) follows the rolling
      GitHub Release - no git operations, no dirty-tree skips;
    - info server is a git checkout: fetch + ff-only pull;
    - the reader runs no updater of its own - the info server's
      pull updates the whole repo on disk and the reader restarts
      itself when its code changed (discord_reader.py)."""
    role = str(
        getattr(getattr(cfg, "pipeline", None), "role", "")
        or "consumer"
    )
    if role == "consumer":
        from core.ops.release_updater import ReleaseUpdater

        return ReleaseUpdater(
            cfg, root, webhook_url, restart, restart_files
        )
    return AutoUpdater(cfg, root, webhook_url, restart, restart_files)


def update_status_payload(app):
    updater = getattr(app, "ws_updater", None)
    if updater is None:
        return {"status": "disabled"}
    return {
        "status": "active",
        "interval_seconds": (
            getattr(updater.cfg.auto_update, "interval_seconds", 600)
        ),
        "last_check": updater.last_check,
        "result": updater.last_result,
        "errors": updater.errors,
        "head": (updater.start_head or "")[:8],
        "branch": updater.branch,
        "last_pull": updater.last_pull(),
    }


