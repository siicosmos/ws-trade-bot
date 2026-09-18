"""Wealthsimple GraphQL field discovery via validation errors.

Introspection is disabled on their endpoint, but the validator
tells us what exists: a query with an unknown field fails with
"Cannot query field 'x' on type 'Y'" (often with "Did you mean"
suggestions naming real fields), while a known field returns its
actual value. We probe a candidate list of margin / buying-power /
multi-leg field names and report what exists.

Runs once shortly after the pipeline starts; results go to the
log and the update webhook.
"""
import re
import threading
import time

FINANCIAL_FIELDS = [
    # control - known to exist, validates the probe itself
    "totalValue",
    # candidates
    "buyingPower",
    "availableToWithdraw",
    "marginAvailable",
    "marginUsed",
    "marginRequirement",
    "maintenanceRequirement",
    "combinedMarginRequirement",
    "excessLiquidity",
    "netLiquidationValue",
    "availableCash",
    "cash",
    "optionLevel",
    "marginBalance",
    "dayTradingBuyingPower",
    "overnightBuyingPower",
    "maintenanceExcess",
    "sma",
]

POSITION_FIELDS = [
    # control
    "id",
    # candidates
    "strategy",
    "positionGroup",
    "groupId",
    "comboId",
    "legs",
    "spread",
    "multiLeg",
    "strategyType",
    "marginRequirement",
]

FINANCIAL_PROBE = """
query ProbeField($identityId: ID!, $currency: Currency!, $accountIds: [ID!]) {
  identity(id: $identityId) {
    financials(filter: {accounts: $accountIds}) {
      current(currency: $currency) {
        FIELD
      }
    }
  }
}
"""

POSITION_PROBE = """
query ProbeField($identityId: ID!, $accountIds: [ID!], $first: Int) {
  identity(id: $identityId) {
    financials(filter: {accounts: $accountIds}) {
      current {
        positions(first: $first) {
          edges { node { FIELD } }
        }
      }
    }
  }
}
"""

SUGGESTION_RE = re.compile(r"Did you mean ['\"]([\w]+)['\"]")


def _fmt(value):
    if isinstance(value, dict):
        amount = value.get("amount")
        currency = value.get("currency")
        if amount is not None:
            return f"{amount} {currency or ''}".strip()
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value[:5]) + "]"
    return str(value)


def _probe_field(ws, template, field, variables, path):
    query = template.replace("FIELD", field)
    try:
        result = ws.graphql_query("ProbeField", query, variables) or {}
    except Exception as e:
        msg = str(e)
        if "Cannot query field" in msg or "Unknown field" in msg:
            hint = SUGGESTION_RE.search(msg)
            return None, (f"no (did you mean '{hint.group(1)}'?)"
                          if hint else "no")
        return None, f"error: {msg[:200]}"
    node = result.get("data") or {}
    for key in path:
        if isinstance(node, list):
            try:
                node = node[key] if isinstance(key, int) else None
            except (IndexError, TypeError):
                node = None
        elif isinstance(node, dict):
            node = node.get(key)
        else:
            node = None
        if node is None:
            break
    if node is None:
        return None, "yes (no value returned)"
    if isinstance(node, list) and not node:
        return None, "yes (no positions)"
    return node, "yes"


def probe_schema(account):
    """Return a text report of which candidate fields exist."""
    try:
        ws = account._client()
    except Exception as e:
        return f"schema probe: client unavailable ({e})"
    identity_id = getattr(ws, "identity_id", None)
    if not identity_id:
        try:
            ws._fetch_identity_id_from_token()
        except Exception:
            pass
        identity_id = getattr(ws, "identity_id", None)
    if not identity_id:
        return "schema probe: no identity id available"

    account_ids = None
    resolve = getattr(account, "_resolve", None)
    if callable(resolve):
        try:
            account_ids = [
                aid for _, aid in resolve() if aid
            ] or None
        except Exception:
            account_ids = None

    lines = []
    control = _probe_field(
        ws, FINANCIAL_PROBE, "totalValue",
        {"identityId": identity_id, "currency": "CAD",
         "accountIds": account_ids},
        ["identity", "financials", "current", "totalValue"],
    )
    if control[0] is None and control[1].startswith("error"):
        return f"schema probe: financials query broken - {control[1]}"

    lines.append("financials.current:")
    for field in FINANCIAL_FIELDS:
        variables = {"identityId": identity_id, "currency": "CAD",
                     "accountIds": account_ids}
        value, status = _probe_field(
            ws, FINANCIAL_PROBE, field, variables,
            ["identity", "financials", "current", field],
        )
        if value is not None:
            lines.append(f"  {field} = {_fmt(value)}")
        else:
            lines.append(f"  {field}: {status}")

    lines.append("position:")
    for field in POSITION_FIELDS:
        variables = {"identityId": identity_id, "accountIds": account_ids,
                     "first": 1}
        value, status = _probe_field(
            ws, POSITION_PROBE, field, variables,
            ["identity", "financials", "current", "positions", "edges",
             0, "node", field],
        )
        if value is not None:
            lines.append(f"  {field} = {_fmt(value)}")
        else:
            lines.append(f"  {field}: {status}")
    return "\n".join(lines)


MAX_MESSAGE = 1800


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
