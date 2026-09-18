"""Wealthsimple GraphQL field discovery via the library document.

Free-form queries are rejected (UNPROCESSABLE_ENTITY) even when
shaped exactly like the library's, so this probe starts from the
library's own FetchIdentityPositions document verbatim - the one
query proven to work in production - and inserts one candidate
field at a time:

  control A: the untouched document (verifies graphql access)
  control B: document + totalValue in `current` (verifies that
             modified documents are accepted at all)

If B works, every margin / buying-power / multi-leg candidate is
probed the same way: unknown fields fail with "Cannot query field"
(plus did-you-mean suggestions), known ones return values.

Runs once shortly after the pipeline starts; results go to the
log and the update webhook.
"""
import re
import threading
import time

# Verbatim from wealthsimple_python.client.get_positions - keep in
# sync if the library updates.
LIB_DOC = """
        query FetchIdentityPositions($identityId: ID!, $currency: Currency!, $first: Int, $cursor: String,
                                     $accountIds: [ID!], $aggregated: Boolean, $currencyOverride: CurrencyOverride,
                                     $filter: PositionFilter, $includeSecurity: Boolean = false) {
          identity(id: $identityId) {
            id
            financials(filter: {accounts: $accountIds}) {
              current(currency: $currency) {
                id
                positions(first: $first, after: $cursor, aggregated: $aggregated, filter: $filter) {
                  edges {
                    node {
                      id quantity percentageOfAccount positionDirection
                      bookValue { amount currency __typename }
                      averagePrice { amount currency __typename }
                      marketAveragePrice: averagePrice(currencyOverride: $currencyOverride) { amount currency __typename }
                      marketBookValue: bookValue(currencyOverride: $currencyOverride) { amount currency __typename }
                      totalValue(currencyOverride: $currencyOverride) { amount currency __typename }
                      unrealizedReturns { amount currency __typename }
                      marketUnrealizedReturns: unrealizedReturns(currencyOverride: $currencyOverride) { amount currency __typename }
                      security {
                        id securityType currency status logoUrl features
                        stock @include(if: $includeSecurity) {
                          name symbol primaryExchange primaryMic __typename
                        }
                        optionDetails @include(if: $includeSecurity) {
                          strikePrice optionType expiryDate osiSymbol multiplier maturity
                          underlyingSecurity {
                            id
                            stock { name symbol primaryExchange __typename }
                            __typename
                          }
                          __typename
                        }
                        quoteV2(currency: null) @include(if: $includeSecurity) {
                          securityId currency price sessionPrice ask bid quotedAsOf previousBaseline __typename
                        }
                        __typename
                      }
                      __typename
                    }
                    __typename
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

CURRENT_ANCHOR = "current(currency: $currency) {\n                id\n"
NODE_ANCHOR = "id quantity percentageOfAccount positionDirection\n"

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


def _run(ws, doc, variables, path):
    """Run one document and extract the value at path."""
    try:
        result = ws.graphql_query("FetchIdentityPositions", doc, variables)
    except Exception as e:
        msg = str(e)
        if "Cannot query field" in msg or "Unknown field" in msg:
            hint = SUGGESTION_RE.search(msg)
            return None, (f"no (did you mean '{hint.group(1)}'?)"
                          if hint else "no")
        if "must have a selection set" in msg:
            return None, "yes (object type)"
        return None, f"error: {msg[:160]}"
    node = (result or {}).get("data") or {}
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
    variables = {
        "identityId": identity_id,
        "currency": "CAD",
        "accountIds": account_ids,
        "first": 500,
        "cursor": None,
        "aggregated": False,
        "currencyOverride": "MARKET",
        "filter": None,
        "includeSecurity": False,
    }

    lines = []
    edges = ["identity", "financials", "current", "positions", "edges"]
    value, status = _run(ws, LIB_DOC, variables, edges)
    if value is None:
        return ("schema probe: control A (library document) failed - "
                f"{status}")
    lines.append(f"control A (library document): {len(value)} positions")

    doc_b = LIB_DOC.replace(
        CURRENT_ANCHOR, CURRENT_ANCHOR + "                totalValue\n"
    )
    fin_path = ["identity", "financials", "current", "totalValue"]
    value, status = _run(ws, doc_b, variables, fin_path)
    if value is None:
        return ("schema probe: control B (document + totalValue) failed - "
                f"{status}\n" + "\n".join(lines) +
                "\nmodified documents appear rejected")
    lines.append(f"control B (document + totalValue): {_fmt(value)}")

    lines.append("financials.current:")
    for field in FINANCIAL_FIELDS:
        doc = LIB_DOC.replace(
            CURRENT_ANCHOR, CURRENT_ANCHOR + "                " + field + "\n"
        )
        value, status = _run(
            ws, doc, variables, fin_path[:-1] + [field]
        )
        if value is not None:
            lines.append(f"  {field} = {_fmt(value)}")
        else:
            lines.append(f"  {field}: {status}")

    lines.append("position:")
    node_path = edges + [0, "node"]
    for field in POSITION_FIELDS:
        doc = LIB_DOC.replace(
            NODE_ANCHOR, NODE_ANCHOR + "                      " + field + "\n"
        )
        value, status = _run(ws, doc, variables, node_path + [field])
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
