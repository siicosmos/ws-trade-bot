@echo off
cd /d %~dp0..\reader

if not exist .venv (
  echo creating venv...
  python -m venv .venv
)

echo checking dependencies...
.venv\Scripts\pip.exe install -q -r requirements-windows.txt
if errorlevel 1 (
  echo warning: pip install failed - verifying existing dependencies
  .venv\Scripts\python.exe -c "import uiautomation, psutil, requests"
  if errorlevel 1 (
    echo dependencies missing - check network and rerun
    pause
    exit /b 1
  )
)

if not exist reader_config.json (
  copy reader_config.example.json reader_config.json
  echo created reader_config.json - run inspect_discord.py and set channel_marker
)

.venv\Scripts\python.exe discord_reader.py
