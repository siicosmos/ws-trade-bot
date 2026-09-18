@echo off
cd /d %~dp0

if not exist .venv (
  echo creating venv and installing dependencies...
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements-windows.txt
)

if not exist reader_config.json (
  copy reader_config.example.json reader_config.json
  echo created reader_config.json - run inspect_discord.py and set channel_marker
)

.venv\Scripts\python.exe discord_reader.py
