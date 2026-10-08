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

_html_mtime = None


def info_html():
    """The info page html, re-read when the file changed on
    disk - a git pull on the shared checkout must not leave the
    served page older than the js it ships with."""
    global _html_mtime, INFO_HTML
    import os as _os

    try:
        mtime = _os.path.getmtime(
            _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                          "static", "info.html")
        )
    except OSError:
        mtime = None
    if _html_mtime != mtime:
        _html_mtime = mtime
        INFO_HTML = _read("info.html")
    return INFO_HTML


_css_mtime = None


def info_css():
    """The shared stylesheet, same live-reload as the html (it
    is inlined into the page head)."""
    global _css_mtime, INFO_CSS
    import os as _os

    try:
        mtime = _os.path.getmtime(
            _os.path.join(_CORE_STATIC, "dashboard.css")
        )
    except OSError:
        mtime = None
    if _css_mtime != mtime:
        _css_mtime = mtime
        INFO_CSS = _read("dashboard.css", _CORE_STATIC)
    return INFO_CSS