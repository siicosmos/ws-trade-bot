"""The wealthsimple client sends its requests with no timeout -
a stalled connection pins the calling thread forever (it wedged
the dashboard's waitress pool once and made every background
refresher hang). Installing this shim injects a hard timeout
into every request the client makes."""

import requests as _requests

WS_HTTP_TIMEOUT = 15.0
_installed = False


def install():
    """Wrap the wealthsimple client module's requests.post so
    every graphql/auth call carries the hard timeout. Idempotent
    and silent when the client is not installed."""
    global _installed
    if _installed:
        return
    try:
        from wealthsimple_python import client as _client_mod
    except ImportError:
        return
    real_post = _requests.post

    def post(*args, **kwargs):
        kwargs.setdefault("timeout", WS_HTTP_TIMEOUT)
        return real_post(*args, **kwargs)

    _client_mod.requests = type(
        "requests_shim", (), {"post": staticmethod(post)}
    )
    _installed = True