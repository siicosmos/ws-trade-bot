"""Consumer dashboard assets.

The HTML/JS live in consumer/static/ - editable as real files,
lintable, syntax-highlighted. The stylesheet is the site-wide
shared one (core/static/dashboard.css) - the info server's page
uses the same design language. The login page lives in
core.web_common (it is shared with the info server).
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


DASHBOARD_HTML = _read("dashboard.html")
DASHBOARD_CSS = _read("dashboard.css", _CORE_STATIC)
DASHBOARD_JS = _read("dashboard.js")

# the login page is shared with the info server (core.web_common)
from core.web_common import LOGIN_HTML  # noqa: E402, F401