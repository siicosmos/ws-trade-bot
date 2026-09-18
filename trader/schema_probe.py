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
    """Verify the app document works and report what it exposes."""
    try:
        ws = account._client()
    except Exception as e:
        return f"schema probe: client unavailable ({e})"
    from trader.ws_positions_query import (
        FETCH_IDENTITY_POSITIONS, app_positions_variables,
    )

    variables = app_positions_variables(ws, None)
    if not variables.get("identityId"):
        return "schema probe: no identity id available"
    try:
        result = ws.graphql_query(
            "FetchIdentityPositions",
            FETCH_IDENTITY_POSITIONS,
            variables,
        )
    except Exception as e:
        return (f"schema probe: app document rejected - {str(e)[:200]}")
    try:
        positions = (
            ((result.get("data") or {}).get("identity") or {})
            .get("financials") or {}
        ).get("current", {}).get("positions", {})
        edges = positions.get("edges") or []
    except Exception:
        edges = []
    lines = [f"app document: ok, {len(edges)} positions"]
    for e in edges[:8]:
        node = e.get("node") or {}
        sec = node.get("security") or {}
        od = sec.get("optionDetails") or {}
        stock = (sec.get("stock") or {}).get("symbol")
        label = (
            f"{stock} {od.get('strikePrice', '')}"
            f"{str(od.get('optionType') or '')[0:5]}"
            if od else (stock or node.get("id", "?"))
        )
        margin = node.get("marginRequirement") or {}
        lines.append(
            f"  {label}: qty={node.get('quantity')}, "
            f"strategy={node.get('strategyType') or '-'}, "
            f"legs={len(node.get('legs') or [])}, "
            f"margin={margin.get('amount', '-')} "
            f"{margin.get('currency', '')}".strip()
        )
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
