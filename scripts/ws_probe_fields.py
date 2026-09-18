"""Probe the Wealthsimple GraphQL schema for margin-related fields.

Run on the machine with ws_tokens.env (or after scripts/ws_login.py).
Uses validation errors to discover fields (introspection is disabled
on their endpoint): an unknown field fails with "Cannot query field"
plus suggestions, a known one returns its actual value.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trader.ws_tokens import load_env_tokens
from trader.schema_probe import probe_schema


def main():
    from wealthsimple_python import WealthsimpleV2

    load_env_tokens()
    ws = WealthsimpleV2()
    try:
        ws.refresh_access_token()
    except Exception as e:
        print(f"token refresh failed ({e}) - trying stored token anyway")

    class Account:
        def _client(self):
            return ws

    print(probe_schema(Account()))


if __name__ == "__main__":
    main()
