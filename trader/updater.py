import json
import os
import subprocess
import threading
import time


UPDATE_RECORD = ".last_update.json"


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=120
    )


class AutoUpdater:
    def __init__(self, cfg, root, webhook_url="", restart=None):
        self.cfg = cfg
        self.root = root
        self.webhook_url = webhook_url
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
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

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

    def _restart_for_local_change(self):
        new = self._head()
        from .notify import notify_discord

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

    def _run(self):
        while True:
            interval = max(30, int(self.cfg.auto_update.interval_seconds))
            deadline = time.time() + interval
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    break
                time.sleep(min(15, remain))
                try:
                    if self._local_head_changed():
                        self._restart_for_local_change()
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
        if status.stdout.strip():
            self.last_result = "skipped: working tree dirty"
            print("auto-update: working tree dirty, skipping pull")
            return False

        branch = _git(
            self.root, "rev-parse", "--abbrev-ref", "HEAD"
        ).stdout.strip()
        short = branch if branch and branch != "HEAD" else "main"
        ref = f"origin/{short}"

        _git(self.root, "fetch", "origin")

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

        from .notify import notify_discord

        notify_discord(
            self.webhook_url,
            "Pipeline restarting",
            {
                "reason": f"auto-updated to {new[:8]}",
                "commits": commits[:1000] or "-",
            },
            ok=True,
        )

        print("auto-update: restarting pipeline...")
        self._record_update("auto")
        self._restart()
        return True
