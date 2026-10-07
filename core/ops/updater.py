import json
import os
import subprocess
import threading
import time


UPDATE_RECORD = ".last_update.json"


PIPELINE_RESTART_FILES = ("trader/*", "run.py", "requirements.txt")
READER_RESTART_FILES = ("reader/*", "requirements.txt")
# per-role globs: each app restarts only when code it executes
# changed (the shared core moves both)
INFO_RESTART_FILES = ("core/*", "info/*", "run.py", "requirements.txt")
CONSUMER_RESTART_FILES = ("core/*", "consumer/*", "run.py",
                          "requirements.txt")


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
    if age < 300:
        return False   # possibly live - hands off
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
    "pipeline.log*", "reader.log*", "trades.db*", "*.db",
    ".last_update.json", ".reader_seen.json", ".session_key",
    "config.yaml", "ws_tokens.env", "*.pyc", "__pycache__/*",
)


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
    head = _git(root, "rev-parse", "--short", "HEAD")
    commit = head.stdout.strip() if head.returncode == 0 else ""
    subject = _git(root, "log", "-1", "--pretty=%s")
    line = f"{name} starting - commit {commit or '?'}"
    if subject.returncode == 0 and subject.stdout.strip():
        line += f' "{subject.stdout.strip()}"'
    try:
        with open(os.path.join(root, UPDATE_RECORD)) as f:
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
        self.webhook_url = webhook_url
        self.restart_files = restart_files or PIPELINE_RESTART_FILES
        self._restart = restart or (lambda: os._exit(77))
        self._thread = None
        self.last_check = None
        self.last_result = "not checked yet"
        self.errors = 0
        self.start_head = self._head()
        self.branch = ""
        r = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode == 0:
            self.branch = r.stdout.strip()
        self._seed_record_if_missing()

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

    def _local_head_changed(self):
        current = self._head()
        return bool(current and self.start_head and current != self.start_head)

    def _record_update(self, how):
        new = self._head()
        try:
            with open(
                os.path.join(self.root, UPDATE_RECORD), "w"
            ) as f:
                json.dump(
                    {
                        "how": how,
                        "commit": new[:8] if new else "?",
                        "ts": time.time(),
                    },
                    f,
                )
        except OSError:
            pass

    def last_pull(self):
        try:
            with open(os.path.join(self.root, UPDATE_RECORD)) as f:
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
            with open(
                os.path.join(self.root, UPDATE_RECORD), "w"
            ) as f:
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
        healed = []
        for line in dirty_lines:
            path = line[3:].strip('"').strip()
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
        notify_discord(
            self.webhook_url,
            "Pipeline restarting",
            {
                "reason": f"code updated to {new[:8] if new else '?'}",
                "commits": commit[:1000] or "-",
            },
            ok=True,
        )
        print("auto-update: local code changed - restarting pipeline...")
        self.last_result = f"local change: {new[:8] if new else '?'}"
        self._restart()
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
                    if self._local_head_changed():
                        # only leave the loop when a restart was
                        # actually initiated; a docs-only local
                        # change must not kill the update thread
                        if self._restart_for_local_change():
                            return
                except Exception:
                    pass
            try:
                self.check_once()
            except Exception as e:
                self.errors += 1
                self.last_result = f"error: {e}"
                print(f"auto-update error: {e}")

    def check_once(self) -> bool:
        self.last_check = time.time()
        if not self.cfg.auto_update.enabled:
            self.last_result = "disabled"
            return False

        r = _git(self.root, "rev-parse", "--is-inside-work-tree")
        if r.returncode != 0:
            self.last_result = "not a git repo"
            return False

        status = _git(self.root, "status", "--porcelain")
        # untracked files never conflict with a pull (logs, dbs) -
        # only modifications to tracked files block it
        dirty = [
            line for line in status.stdout.splitlines()
            if line.strip() and not line.startswith("??")
        ]
        if dirty:
            self._heal_ignored_runtime_files(dirty)
            status = _git(self.root, "status", "--porcelain")
            dirty = [
                line for line in status.stdout.splitlines()
                if line.strip() and not line.startswith("??")
            ]
        if dirty:
            names = ", ".join(
                line[3:].strip('"') for line in dirty[:4]
            ) or "?"
            self.last_result = (
                "skipped: working tree dirty (" + names + ")"
            )
            print(
                "auto-update: working tree dirty, skipping pull: "
                + names
            )
            return False

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

        pull = _git(self.root, "pull", "--ff-only", "origin", short)
        if pull.returncode != 0:
            self.last_result = f"pull failed: {pull.stderr.strip()[:120]}"
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
                "Pipeline updated (no restart)",
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
            return False

        notify_discord(
            self.webhook_url,
            "Pipeline restarting",
            {
                "reason": f"code updated to {new[:8]}",
                "commits": commits[:1000] or "-",
            },
            ok=True,
        )

        print("auto-update: restarting pipeline...")
        self._record_update("auto")
        self._restart()
        return True


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


