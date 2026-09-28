import os
import sys
import threading
import time

import requests


class WebhookBatcher:
    def __init__(self, url, flush_seconds=3):
        self.url = url
        self.flush_seconds = flush_seconds
        self.lines = []
        self.lock = threading.Lock()
        if url:
            from .supervise import supervised

            supervised("webhook-batcher", self._run, url)
            # buffered lines must survive the process: the last
            # log ("stopped", a crash traceback) dies with the
            # 3s background flush otherwise - best effort, a
            # ctrl+c inside the flush is swallowed
            import atexit

            def _flush_quiet():
                try:
                    self.flush_now()
                except BaseException:
                    pass

            atexit.register(_flush_quiet)

    def add(self, line):
        with self.lock:
            self.lines.append(str(line)[:500])
            if len(self.lines) > 200:
                self.lines = self.lines[-200:]

    def flush_now(self):
        with self.lock:
            batch, self.lines = self.lines, []
        if not batch or not self.url:
            return
        text = ""
        for line in batch:
            if len(text) + len(line) + 1 > 1900:
                self._post(text)
                text = ""
            text += line + "\n"
        if text.strip():
            self._post(text)

    def _post(self, text):
        try:
            requests.post(
                self.url, json={"content": text[:1900]}, timeout=10
            )
        except requests.RequestException:
            pass

    def _run(self):
        while True:
            time.sleep(self.flush_seconds)
            self.flush_now()


class LogFile:
    """Append-only log with a size cap, kept even when the
    process dies - the crash exit code and traceback end up here."""

    def __init__(self, path, max_bytes=2_000_000, keep=3):
        self.path = path
        self.max_bytes = max_bytes
        self.keep = keep
        self.lock = threading.Lock()

    def add(self, line):
        try:
            with self.lock:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line.rstrip("\n") + "\n")
        except OSError:
            pass

    def _rotate_if_needed(self):
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
            for i in range(self.keep - 1, 0, -1):
                src = f"{self.path}.{i}"
                if os.path.exists(src):
                    os.replace(src, f"{self.path}.{i + 1}")
            os.replace(self.path, f"{self.path}.1")
        except OSError:
            pass


class TeeStream:
    def __init__(self, original, batcher=None, log_file=None):
        self._original = original
        self._batcher = batcher
        self._log_file = log_file
        self._buf = ""

    def write(self, s):
        # line-buffered: every complete line (console, webhook
        # and file copies) is stamped with the log format
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)

    def _emit(self, line):
        stamped = (
            time.strftime("%d/%b/%Y %H:%M:%S") + " " + line
        )
        self._original.write(stamped + "\n")
        if line.strip():
            if self._batcher is not None:
                self._batcher.add(stamped)
            if self._log_file is not None:
                self._log_file.add(stamped)

    def flush(self):
        self._original.flush()
        if self._buf:
            line, self._buf = self._buf, ""
            self._emit(line)

    def __getattr__(self, name):
        return getattr(self._original, name)


def install_log_webhook(url, log_path=None):
    batcher = WebhookBatcher(url) if url else None
    log_file = LogFile(log_path) if log_path else None
    if batcher is None and log_file is None:
        return None
    sys.stdout = TeeStream(sys.stdout, batcher, log_file)
    sys.stderr = TeeStream(sys.stderr, batcher, log_file)
    return batcher
