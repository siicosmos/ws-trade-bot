"""Info dashboard assets (lean page: consumers, feed, levels)."""

import os

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _read(name):
    with open(os.path.join(_STATIC, name), encoding="utf-8") as f:
        return f.read()


INFO_HTML = _read("info.html")
INFO_CSS = _read("dashboard.css")