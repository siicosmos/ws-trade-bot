"""Error text for logs, dashboard state, and Discord webhooks.

Auth tokens travel in request headers, but error text that ends
up in logs, dashboard state, or Discord webhooks must never
carry them - an exception message can embed the request URL
(with credentials) or other request details, and the log tail
is posted to Discord verbatim. redact() scrubs known secrets
from any string before it is stored, printed, or reported.

format_error() is the standard broad-catch rendering: exception
type + message lead (a truncated consumer keeps the
identification), the traceback follows for the log.
"""

from urllib.parse import urlsplit, urlunsplit

import logging
import traceback

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


def short_error(e, *secrets):
    """The compact rendering for one-line consumers - dashboard
    cells, trade-row details, stored error state: `Type: message`
    with secrets scrubbed, no traceback (a traceback in a ui cell
    is noise; the loops log the full format_error rendering)."""
    return redact(f"{type(e).__name__}: {e}", *secrets)


def format_error(e, *secrets):
    """The standard broad-catch error text: `Type: message` leads
    (a truncated consumer - a webhook field, a dashboard cell -
    keeps the identification), the traceback follows for the
    log. Secrets are scrubbed from everything: the traceback's
    final line repeats the message verbatim, so a token in the
    message would otherwise ride into the log. A caller whose
    exception text can carry a https://user:pass@host url scrubs
    the result with redact_url (userinfo shorter than
    MIN_SECRET_LEN survives a plain substring replace)."""
    return redact(f"{short_error(e)}\n{traceback.format_exc()}",
                  *secrets)
