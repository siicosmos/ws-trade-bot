"""Web machinery shared by both apps: request-log quieting,
gzip, session auth (login/logout + the machine-token guard).

The info server and the consumer app each build their own
Flask app; this module holds the parts that would otherwise be
duplicated.
"""

import gzip
import hmac
import logging
import os
import secrets
import time

from flask import Response, jsonify, redirect, request, session


QUIET_GET_PATHS = (
    "/",
    "/health",
    "/favicon.ico",
    "/api/summary",
    "/api/positions",
    "/api/signals",
    "/api/trades",
    "/api/settings",
    "/api/reader_status",
    "/api/update_status",
    "/api/feed-status",
)

QUIET_POST_PATHS = (
    "/api/reader_status",
)

QUIET_GET_PREFIXES = (
    "/.well-known/",
)


class QuietPathsFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for path in QUIET_POST_PATHS:
            if f" {path} HTTP" in msg:
                return False
        for path in QUIET_GET_PATHS:
            if f"GET {path} HTTP" in msg:
                return False
        for prefix in QUIET_GET_PREFIXES:
            if f"GET {prefix}" in msg:
                return False
        return True


def install_quiet_filter():
    werkzeug = logging.getLogger("werkzeug")
    if not any(
        isinstance(f, QuietPathsFilter) for f in werkzeug.filters
    ):
        werkzeug.addFilter(QuietPathsFilter())


LOGIN_FAIL_LIMIT = 5
LOCKOUT_SECONDS = 900
_LOGIN_FAILS = {}


def _purge_login_fails(now):
    """Drop fail entries not touched for a day - the dict must
    not grow without bound under sustained attacks. (A zero
    locked_until means 'not locked yet', not 'long expired'.)"""
    stale = [
        ip for ip, e in _LOGIN_FAILS.items()
        if now - e.get("ts", 0) > 86400
    ]
    for ip in stale:
        _LOGIN_FAILS.pop(ip, None)


def load_secret_key(config_path):
    if not config_path:
        return secrets.token_hex(32)
    # the consumer's login-session signing key - lives with the
    # consumer app, named for the role (the info server has no
    # sessions)
    key_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "consumer", ".consumer_session_key"
    )
    try:
        with open(key_file, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(key)
        # the session signing key is only for this user
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


def LOGIN_HTML(error=None):
    import html

    intro = "<p>log in to continue</p>"

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
    <button type="submit">Log in</button>
  </form>
</div>
</body>
</html>"""


def install_gzip(app):
    @app.after_request
    def no_store(resp):
        # unconditional: flask's static handler sets no-cache,
        # and the page must never serve stale after an update
        resp.headers["Cache-Control"] = "no-store"
        # gzip the text payloads (the dashboard js alone is
        # ~80 kb and phones on the lan pull it every reload)
        try:
            compressible = (
                "text/" in resp.content_type
                or "javascript" in resp.content_type
                or resp.content_type == "application/json"
            )
            accepts = request.headers.get("Accept-Encoding", "")
            if (
                "gzip" in accepts.lower()
                and compressible
                and resp.content_length
                and resp.content_length > 500
            ):
                resp.direct_passthrough = False
                body = gzip.compress(resp.get_data(), 6)
                if len(body) < resp.content_length:
                    resp.set_data(body)
                    resp.headers["Content-Encoding"] = "gzip"
                    resp.headers["Content-Length"] = str(len(body))
            resp.headers.setdefault("Vary", "Accept-Encoding")
        except Exception:
            pass
        return resp
    return no_store


def install_auth(app, cfg, store, login_html, exempt_paths=()):
    """Session + machine-token auth: the before_request guard, the
    login route and logout. exempt_paths skip the guard (they do
    their own auth - the info server's consumer-token feed)."""
    exempt = set(exempt_paths)
    app._last_reject_log = 0.0

    @app.before_request
    def auth_guard():
        token = cfg.pipeline.auth_token
        if request.path in ("/health", "/favicon.ico", "/login"):
            return None
        if request.path in exempt:
            return None
        if session.get("auth"):
            return None
        supplied = request.headers.get("X-Auth-Token", "")
        if supplied and token and hmac.compare_digest(
            supplied.encode("utf-8", "ignore"),
            token.encode("utf-8", "ignore"),
        ):
            from flask import g

            g.admin = True
            return None
        if request.path.startswith("/api/") or request.path == "/alert":
            # a rejected machine token (e.g. the info server pushing
            # with a stale consumers[] token after the consumer's
            # auth_token changed) used to be fully silent
            now = time.time()
            if now - app._last_reject_log > 60:
                app._last_reject_log = now
                app.logger.warning(
                    "request rejected (bad or missing X-Auth-Token) "
                    "on %s %s - the caller's token must match this "
                    "app's auth_token",
                    request.method, request.path,
                )
            return jsonify({"error": "unauthorized"}), 401
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        ip = request.remote_addr or "?"
        now = time.time()
        _purge_login_fails(now)
        entry = _LOGIN_FAILS.get(ip)
        if entry and entry.get("locked_until", 0) > now:
            return Response(
                login_html("too many attempts - try again later"),
                403,
                mimetype="text/html",
                headers={"Cache-Control": "no-store"},
            )
        error = None
        if request.method == "POST":
            username = str(request.form.get("username") or "").strip()
            supplied = request.form.get("password", "")

            def _fail(label):
                nonlocal error
                count = (entry or {}).get("count", 0) + 1
                if count >= LOGIN_FAIL_LIMIT:
                    _LOGIN_FAILS[ip] = {
                        "count": count,
                        "locked_until": now + LOCKOUT_SECONDS,
                        "ts": now,
                    }
                    error = "too many attempts - try again later"
                else:
                    _LOGIN_FAILS[ip] = {
                        "count": count, "locked_until": 0, "ts": now,
                    }
                    error = label
                time.sleep(1)

            user = store.verify_user(username, supplied)
            if user is not None:
                _LOGIN_FAILS.pop(ip, None)
                session.permanent = True
                session["auth"] = True
                session["user"] = user
                return redirect("/")
            _fail("wrong username or password")
        return Response(
            login_html(error), mimetype="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")