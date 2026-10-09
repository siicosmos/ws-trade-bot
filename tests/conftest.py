"""Pytest bootstrap for the test suite.

The shared stubs live in _helpers.py (a regular module - conftest
is a pytest plugin and should not double as an import target).
This file puts the repo root and this directory on sys.path so
both `core.*` and the flat test helpers resolve everywhere.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _helpers  # noqa: F401,E402  (validates the path setup)
