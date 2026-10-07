"""Consumer dashboard assets.

The HTML/CSS/JS live in consumer/static/ - editable as real
files, lintable, syntax-highlighted. This module loads them and
re-exports the pieces the server and tests use; the login page
stays here (it is a tiny parameterized template).
"""

import os

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _read(name):
    with open(os.path.join(_STATIC, name), encoding="utf-8") as f:
        return f.read()


DASHBOARD_HTML = _read("dashboard.html")
DASHBOARD_CSS = _read("dashboard.css")
DASHBOARD_JS = _read("dashboard.js")

# the login page is shared with the info server (core.web_common)
from core.web_common import LOGIN_HTML  # noqa: E402, F401


