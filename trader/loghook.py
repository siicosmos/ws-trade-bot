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
            threading.Thread(target=self._run, daemon=True).start()

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


class TeeStream:
    def __init__(self, original, batcher):
        self._original = original
        self._batcher = batcher

    def write(self, s):
        self._original.write(s)
        stripped = s.rstrip("\n")
        if stripped.strip():
            self._batcher.add(stripped)

    def flush(self):
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)


def install_log_webhook(url):
    if not url:
        return
    batcher = WebhookBatcher(url)
    sys.stdout = TeeStream(sys.stdout, batcher)
    sys.stderr = TeeStream(sys.stderr, batcher)
    return batcher
