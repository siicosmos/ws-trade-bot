"""The wealthsimple client sends its requests with no timeout -
a stalled connection pins the calling thread forever (it wedged
the dashboard's waitress pool once and made every background
refresher hang). Installing this shim injects a hard timeout
into every request the client makes.

Two layers, so the client cannot route around it:

- every module-level verb (post, get, and any verb the client
  adds later - put, delete, patch, request, ...) is resolved
  dynamically and wrapped with the timeout;
- requests.Session resolves to a subclass whose HTTPAdapter
  enforces the timeout at the transport layer, so even a
  session the client builds itself is covered.
"""

import functools
import inspect

import requests as _requests

WS_HTTP_TIMEOUT = 15.0
_installed = False


class _TimeoutAdapter(_requests.adapters.HTTPAdapter):
    """Transport-level enforcement: a request that reaches the
    wire without a timeout gets the hard one (an explicit caller
    timeout is never overridden)."""

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = WS_HTTP_TIMEOUT
        return super().send(request, **kwargs)


class _TimeoutSession(_requests.Session):
    """A session whose every request carries the hard timeout."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        adapter = _TimeoutAdapter()
        self.mount("https://", adapter)
        self.mount("http://", adapter)


class _RequestsShim:
    """Drop-in for the requests module inside the client: verbs
    are wrapped lazily (a client call to a verb this shim never
    heard of still gets the timeout), classes and constants pass
    through untouched."""

    def __getattr__(self, name):
        if name == "Session":
            return _TimeoutSession
        real = getattr(_requests, name)
        if not callable(real) or inspect.isclass(real):
            return real

        @functools.wraps(real)
        def verb(*args, **kwargs):
            kwargs.setdefault("timeout", WS_HTTP_TIMEOUT)
            return real(*args, **kwargs)

        return verb


def install():
    """Swap the wealthsimple client module's requests global for
    the timeout-enforcing shim. Idempotent and silent when the
    client is not installed."""
    global _installed
    if _installed:
        return
    try:
        from wealthsimple_python import client as _client_mod
    except ImportError:
        return
    _client_mod.requests = _RequestsShim()
    _installed = True