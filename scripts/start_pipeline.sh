#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  echo "creating venv..."
  python3 -m venv .venv
fi

echo "checking dependencies..."
if ! .venv/bin/pip install --quiet -r requirements.txt; then
  echo "warning: pip install failed - verifying existing dependencies"
  .venv/bin/python -c "import flask, yaml, requests" || {
    echo "dependencies missing - check network and rerun"
    exit 1
  }
fi

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "created config.yaml - edit it before going live"
fi

if [ -f ws_tokens.env ]; then
  set -a
  . ./ws_tokens.env
  set +a
  echo "loaded Wealthsimple tokens from ws_tokens.env"
fi

exec .venv/bin/python run.py -c config.yaml
