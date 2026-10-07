"""Info dashboard assets (lean page: consumers, feed, levels).

The stylesheet is the site-wide shared one (core/static/
dashboard.css) - the info page uses the same design language as
the consumer dashboard.
"""

import os

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_CORE_STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core", "static",
)


def _read(name, root=None):
    base = root or _STATIC
    with open(os.path.join(base, name), encoding="utf-8") as f:
        return f.read()


INFO_HTML = _read("info.html")
INFO_CSS = _read("dashboard.css", _CORE_STATIC)