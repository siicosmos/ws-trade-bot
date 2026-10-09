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

_html_mtime = None
_css_mtime = None


def dashboard_html():
    """The dashboard html, re-read when the file changed on
    disk - a release swap (or a git pull on a shared checkout)
    must not leave the served page older than the js it ships
    with (the old page lacks the new markup and the new js
    errors against it)."""
    global _html_mtime, DASHBOARD_HTML
    try:
        mtime = os.path.getmtime(os.path.join(_STATIC, "dashboard.html"))
    except OSError:
        mtime = None
    if _html_mtime != mtime:
        _html_mtime = mtime
        DASHBOARD_HTML = _read("dashboard.html")
        # cache-bust the script tag: the html re-reads on change,
        # and the new url forces browsers past any cached js
        DASHBOARD_HTML = DASHBOARD_HTML.replace(
            'src="/static/dashboard.js"',
            f'src="/static/dashboard.js?v={int(mtime or 0)}"',
        )
    return DASHBOARD_HTML


def dashboard_css():
    """The shared stylesheet, same live-reload as the html (it
    is inlined into the page head)."""
    global _css_mtime, DASHBOARD_CSS
    try:
        mtime = os.path.getmtime(os.path.join(_CORE_STATIC, "dashboard.css"))
    except OSError:
        mtime = None
    if _css_mtime != mtime:
        _css_mtime = mtime
        DASHBOARD_CSS = _read("dashboard.css", _CORE_STATIC)
    return DASHBOARD_CSS

# the login page is shared with the info server (core.web_common)
from core.web_common import LOGIN_HTML  # noqa: E402, F401

# served from static/ (the route reads the file live); the module
# attribute exists for the tests that assert on the script's
# content
DASHBOARD_JS = _read("dashboard.js")