"""Dashboard assets.

The HTML/CSS/JS live in trader/web/static/ (plan #7) - editable
as real files, lintable, syntax-highlighted. This module loads
them and re-exports the pieces the server and tests use; the
login page stays here (it is a tiny parameterized template).
"""

import os

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _read(name):
    with open(os.path.join(_STATIC, name), encoding="utf-8") as f:
        return f.read()


DASHBOARD_HTML = _read("dashboard.html")
DASHBOARD_CSS = _read("dashboard.css")
DASHBOARD_JS = _read("dashboard.js")
INFO_HTML = _read("info.html")
INFO_JS = _read("info.js")


def LOGIN_HTML(error=None, first=False):
    import html

    intro = (
        "<p>create the first admin account to secure this "
        "dashboard</p>" if first else "<p>log in to continue</p>"
    )

    message = (
        f'<p style="color:#f85149;margin:0 0 14px">{html.escape(str(error))}</p>'
        if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WS Trade Bot - log in</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #0d1117;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: #e6edf3;
  }}
  .card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 32px; width: 320px;
  }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  p {{ color: #8b949e; font-size: 12px; margin: 0 0 20px; }}
  input {{
    width: 100%; box-sizing: border-box; background: #0d1117;
    color: #e6edf3; border: 1px solid #30363d; border-radius: 6px;
    padding: 8px 10px; font-size: 14px; margin-bottom: 12px;
  }}
  input:focus {{ outline: none; border-color: #58a6ff; }}
  button {{
    width: 100%; background: #238636; color: #fff; border: 1px solid #2ea043;
    border-radius: 6px; padding: 8px 10px; font-size: 14px; font-weight: 600;
    cursor: pointer;
  }}
  button:hover {{ background: #2ea043; }}
</style>
</head>
<body>
<div class="card">
  <h1>WS Trade Bot</h1>
  {intro}
  {message}
  <form method="post" action="/login">
    <input type="text" name="username" placeholder="username" autocomplete="username" autofocus>
    <input type="password" name="password" placeholder="password" autocomplete="current-password">
    <button type="submit">{'Create admin account' if first else 'Log in'}</button>
  </form>
</div>
</body>
</html>"""
