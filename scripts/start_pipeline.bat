@echo off
cd /d %~dp0..

if not exist .venv (
  echo creating venv...
  python -m venv .venv
)

echo checking dependencies...
.venv\Scripts\pip.exe install -q -r requirements.txt
if errorlevel 1 (
  echo warning: pip install failed - verifying existing dependencies
  .venv\Scripts\python.exe -c "import flask, yaml, requests" || (
    echo dependencies missing - check network and rerun
    pause
    exit /b 1
  )
)

if not exist config.yaml (
  copy config.example.yaml config.yaml
  echo created config.yaml - edit it before going live
)

:start
.venv\Scripts\python.exe run.py -c config.yaml
set EXITCODE=%errorlevel%
if %EXITCODE% == 0 (
  echo pipeline stopped cleanly
  goto end
)
if %EXITCODE% == -1073741510 (
  echo pipeline stopped via ctrl+c
  goto end
)
echo pipeline exited with code %EXITCODE% - restarting in 5s
echo check pipeline.log next to trades.db for the traceback
ping -n 6 127.0.0.1 >nul
goto start

:end
