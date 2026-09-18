"""Wealthsimple GraphQL field discovery via validation errors.

Introspection is disabled on their endpoint, so fields are
discovered by querying candidate names on the live shapes:
unknown fields fail validation with "Cannot query field" (often
with "Did you mean" suggestions), known ones return values.

The financials query shape that works is probed at runtime: the
library-exact header, a filterless variant, and a null-filtered
variant are tried with a totalValue control and the first that
executes wins.

Runs once shortly after the pipeline starts; results go to the
log and the update webhook.
"""
import re
import threading
import time

FINANCIAL_FIELDS = [
    "totalValue",
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
    "quantity",
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

LIB_HEADER = """
query FetchIdentityPositions($identityId: ID!, $currency: Currency!, $first: Int, $cursor: String,
                             $accountIds: [ID!], $aggregated: Boolean, $currencyOverride: CurrencyOverride,
                             $filter: PositionFilter, $includeSecurity: Boolean = false) {
"""

T_LIB_FIN = LIB_HEADER + """
  identity(id: $identityId) {
    financials(filter: {accounts: $accountIds}) {
      current(currency: $currency) {
        FIELD
        __typename
      }
      __typename
    }
    __typename
  }
}
"""

T_NOFILTER = """
query ProbeField($identityId: ID!, $currency: Currency!) {
  identity(id: $identityId) {
    financials {
      current(currency: $currency) {
        FIELD
      }
    }
  }
}
"""

T_NULLFILTER = """
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

T_LIB_POS = LIB_HEADER + """
  identity(id: $identityId) {
    financials(filter: {accounts: $accountIds}) {
      current(currency: $currency) {
        positions(first: $first, after: $cursor, aggregated: $aggregated, filter: $filter) {
          edges {
            node {
              FIELD
            }
          }
          pageInfo { hasNextPage endCursor __typename }
          totalCount status __typename
        }
        __typename
      }
      __typename
    }
    __typename
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
    return node, "yes"


def _lib_vars(identity_id):
    return {
        "identityId": identity_id,
        "currency": "CAD",
        "accountIds": None,
        "first": 1,
        "cursor": None,
        "aggregated": False,
        "currencyOverride": "MARKET",
        "filter": None,
        "includeSecurity": False,
    }


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

    fin_path = ["identity", "financials", "current", "totalValue"]
    variants = [
        ("lib-null-ids", T_LIB_FIN, _lib_vars(identity_id)),
        ("no-filter", T_NOFILTER,
         {"identityId": identity_id, "currency": "CAD"}),
        ("null-filter", T_NULLFILTER,
         {"identityId": identity_id, "currency": "CAD",
          "accountIds": None}),
    ]
    lines = []
    template = None
    variables = None
    for name, tmpl, vars_ in variants:
        value, status = _probe_field(
            ws, tmpl, "totalValue", vars_, fin_path
        )
        if value is not None:
            lines.append(f"probe template: {name} (control totalValue = "
                         f"{_fmt(value)})")
            template = tmpl
            variables = vars_
            break
        lines.append(f"template {name}: {status}")
    if template is None:
        return "schema probe: no working financials query shape\n" + \
            "\n".join(lines)

    lines.append("financials.current:")
    for field in FINANCIAL_FIELDS:
        value, status = _probe_field(
            ws, template, field, variables, fin_path[:-1] + [field]
        )
        if value is not None:
            lines.append(f"  {field} = {_fmt(value)}")
        else:
            lines.append(f"  {field}: {status}")

    pos_vars = _lib_vars(identity_id)
    pos_path = ["identity", "financials", "current", "positions",
                "edges", 0, "node", "quantity"]
    value, status = _probe_field(
        ws, T_LIB_POS, "quantity", pos_vars, pos_path
    )
    if value is None and not status.startswith("yes"):
        lines.append(f"position probing unavailable ({status})")
    else:
        lines.append("position:")
        for field in POSITION_FIELDS:
            value, status = _probe_field(
                ws, T_LIB_POS, field, pos_vars, pos_path[:-1] + [field]
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
