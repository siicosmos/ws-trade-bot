"""One-shot Wealthsimple GraphQL schema probe.

Introspects candidate types and reports every field name they
expose, so the real margin / buying-power / multi-leg fields can
be identified instead of guessed. Runs once shortly after the
pipeline starts; results go to the log and the update webhook.
"""
import threading
import time

INTROSPECTION = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    fields { name }
  }
}
"""

CANDIDATE_TYPES = [
    "Position",
    "Security",
    "OptionDetails",
    "CustodianAccountCurrentFinancialValues",
    "AccountCurrentFinancials",
    "AccountFundingBalance",
    "Account",
    "CustodianAccount",
    "Identity",
]

MAX_MESSAGE = 1800


def probe_schema(account):
    """Return a text report of field names per candidate type."""
    try:
        ws = account._client()
    except Exception as e:
        return f"schema probe: client unavailable ({e})"
    lines = []
    for type_name in CANDIDATE_TYPES:
        try:
            result = ws.graphql_query(
                "IntrospectType", INTROSPECTION, {"name": type_name}
            )
        except Exception as e:
            lines.append(f"{type_name}: query failed ({e})")
            continue
        t = (result.get("data") or {}).get("__type")
        if not t:
            lines.append(f"{type_name}: not found")
            continue
        names = [f.get("name") for f in (t.get("fields") or [])]
        lines.append(f"{type_name}: {', '.join(n for n in names if n)}")
    return "\n".join(lines)


def _post(webhook_url, report):
    from trader.notify import notify_plain

    chunks = []
    current = []
    size = 0
    for line in report.splitlines():
        if size + len(line) + 1 > MAX_MESSAGE:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    for i, chunk in enumerate(chunks):
        notify_plain(
            webhook_url,
            f"WS schema probe ({i + 1}/{len(chunks)}):\n```\n{chunk}\n```",
        )


def start_background_probe(account, webhook_url=None, delay=45):
    """Probe once in a daemon thread once the session is warm."""

    def run():
        time.sleep(delay)
        try:
            report = probe_schema(account)
        except Exception as e:
            report = f"schema probe failed: {e}"
        print(report)
        if webhook_url:
            try:
                _post(webhook_url, report)
            except Exception as e:
                print(f"schema probe: could not post report ({e})")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
