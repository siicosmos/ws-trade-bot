import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from consumer.app import pick_wsgi


def test_waitress_only_for_plain_http():
    """TLS configs must stay on Werkzeug: waitress has no
    native ssl, and serving plain http under an https config
    kills the health watchdog loop (and the reader's posts)."""
    assert pick_wsgi(None, True) is True
    assert pick_wsgi(("cert.pem", "key.pem"), True) is False
    assert pick_wsgi(("cert.pem", "key.pem"), False) is False
    assert pick_wsgi(None, False) is False
