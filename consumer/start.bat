@echo off
rem consumer app: the trading pipeline fed by the info server's
rem alert feed. everything this role needs lives in this folder:
rem   config.yaml (role: consumer) · trades.db · pipeline.log
cd /d %~dp0

if not exist ..\.venv (
  echo creating venv...
  python -m venv ..\.venv
)

echo checking dependencies...
..\.venv\Scripts\pip.exe install -q -r ..\requirements.txt
if errorlevel 1 (
  echo warning: pip install failed - verifying existing dependencies
  ..\.venv\Scripts\python.exe -c "import flask, yaml, requests" || (
    echo dependencies missing - check network and rerun
    pause
    exit /b 1
  )
)

if not exist config.yaml (
  echo config.yaml missing - run scripts\split_roles.py first
  pause
  exit /b 1
)

:start
..\.venv\Scripts\python.exe ..\run.py -c config.yaml --db trades.db
set EXITCODE=%errorlevel%
if %EXITCODE% == 0 (
  echo consumer app stopped cleanly
  goto end
)
if %EXITCODE% == -1073741510 (
  echo consumer app stopped via ctrl+c
  goto end
)
echo consumer app exited with code %EXITCODE% - restarting in 5s
(echo consumer app exited with code %EXITCODE%)> pipeline_exit.txt
echo check pipeline.log in this folder for the traceback
ping -n 6 127.0.0.1 >nul
goto start

:end
