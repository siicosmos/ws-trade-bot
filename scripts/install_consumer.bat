@echo off
rem first-time install of the consumer client from a release zip.
rem run this once from the unzipped folder: scripts\install_consumer.bat
cd /d %~dp0..

if not exist .venv (
  echo creating venv...
  python -m venv .venv || (
    echo python not found - install python 3.10+ first
    pause
    exit /b 1
  )
)

echo installing dependencies...
.venv\Scripts\pip.exe install -q -r requirements.txt
if errorlevel 1 (
  echo pip install failed - check network and rerun
  pause
  exit /b 1
)

if not exist consumer\consumer.config.yaml (
  copy config\consumer.config.yaml consumer\consumer.config.yaml >nul
  echo created consumer\consumer.config.yaml - edit it:
  echo   pipeline.auth_token, feed url + token,
  echo   auto_update.github_token ^(a read-only GitHub token, needed
  echo   while the repo is private^)
)

if not exist consumer\ws_tokens.env (
  echo add your Wealthsimple tokens to consumer\ws_tokens.env
  echo ^(see config\consumer.config.yaml and scripts\ws_login.py^)
)

echo.
echo install complete - start the app with scripts\start_consumer.bat
pause