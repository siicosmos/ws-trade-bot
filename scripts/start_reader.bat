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
  .venv\Scripts\python.exe -c "import uiautomation, psutil, requests, yaml"
  if errorlevel 1 (
    echo dependencies missing - check network and rerun
    pause
    exit /b 1
  )
)

if not exist ..\config.yaml (
  echo config.yaml not found in repo root - copy config.example.yaml to config.yaml
  pause
  exit /b 1
)

:start
.venv\Scripts\python.exe discord_reader.py
set EXITCODE=%errorlevel%
if %EXITCODE% == 0 (
  echo reader stopped cleanly
  goto end
)
if %EXITCODE% == -1073741510 (
  echo reader stopped via ctrl+c
  goto end
)
echo reader exited with code %EXITCODE% - restarting in 5s
(echo reader exited with code %EXITCODE%)> "%~dp0..\reader_exit.txt"
ping -n 6 127.0.0.1 >nul
goto start

:end
