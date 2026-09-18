#!/usr/bin/env bash
set -e

if [ ! -d .venv ]; then
  echo "creating venv and installing dependencies..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet -r requirements.txt
fi

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "created config.yaml - edit it before going live"
fi

exec .venv/bin/python run.py -c config.yaml
