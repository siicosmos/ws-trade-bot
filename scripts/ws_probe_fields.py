"""Probe the Wealthsimple GraphQL schema for margin-related fields.

Run on the machine with ws_tokens.env (or after scripts/ws_login.py).
Introspects the types behind account financials and funding balances
and prints every field name, so we can query the real margin,
buying-power, and available-to-withdraw fields instead of guessing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.ws_tokens import load_env_tokens

INTROSPECTION = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    fields {
      name
      type { kind name ofType { kind name ofType { name } } }
    }
  }
}
"""

CANDIDATE_TYPES = [
    "CustodianAccountCurrentFinancialValues",
    "AccountCurrentFinancials",
    "AccountFundingBalance",
    "Account",
    "CustodianAccount",
    "Identity",
]


def main():
    from wealthsimple_python import WealthsimpleV2

    load_env_tokens()
    ws = WealthsimpleV2()
    try:
        ws.refresh_access_token()
    except Exception as e:
        print(f"token refresh failed ({e}) - trying stored token anyway")

    for type_name in CANDIDATE_TYPES:
        print(f"\n=== {type_name} ===")
        try:
            result = ws.graphql_query(
                "IntrospectType", INTROSPECTION, {"name": type_name}
            )
        except Exception as e:
            print(f"  query failed: {e}")
            continue
        t = (result.get("data") or {}).get("__type")
        if not t:
            print("  (type not found in schema)")
            continue
        for field in t.get("fields") or []:
            type_info = field.get("type") or {}
            type_name_out = (
                type_info.get("name")
                or (type_info.get("ofType") or {}).get("name")
                or (type_info.get("ofType") or {}).get("ofType", {}) or {}
            ).get("name") if isinstance(
                (type_info.get("ofType") or {}).get("ofType", {}), dict
            ) else None
            print(f"  {field['name']}: {type_name_out or type_info.get('kind')}")


if __name__ == "__main__":
    main()
