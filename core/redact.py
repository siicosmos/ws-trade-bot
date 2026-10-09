"""Secret redaction for error strings.

Auth tokens travel in request headers, but error text that ends
up in logs, dashboard state, or Discord webhooks must never
carry them - an exception message can embed the request URL
(with credentials) or other request details, and the log tail
is posted to Discord verbatim. redact() scrubs known secrets
from any string before it is stored, printed, or reported.
"""

from urllib.parse import urlsplit, urlunsplit

import logging

log = logging.getLogger("redact")


# a secret shorter than this is skipped: replacing a 1-2 char
# token would mangle the surrounding text (every "t" in the
# message) while real tokens are long random strings
MIN_SECRET_LEN = 8


def redact(text, *secrets):
    """Replace every occurrence of each secret with *** - a
    short secret is left alone (see MIN_SECRET_LEN)."""
    out = str(text or "")
    for secret in secrets:
        s = str(secret or "")
        if len(s) >= MIN_SECRET_LEN:
            out = out.replace(s, "***")
    return out


def redact_url(url, *secrets):
    """Scrub userinfo (user:pass@host) and any embedded secrets
    from a URL - safe to print or store."""
    out = str(url or "")
    for secret in secrets:
        s = str(secret or "")
        if len(s) >= MIN_SECRET_LEN:
            out = out.replace(s, "***")
    try:
        parts = urlsplit(out)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host += f":{parts.port}"
            netloc = "***@" + host if parts.username else host
            out = urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query,
                 parts.fragment)
            )
    except ValueError as e:
        # a malformed url (e.g. a bad port) - the text is still
        # token-scrubbed above; note the parse failure at debug
        log.debug("redact_url: urlsplit failed (%s)", e)
    return out
