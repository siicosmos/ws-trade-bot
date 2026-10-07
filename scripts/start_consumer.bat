@echo off
rem consumer app: the trading pipeline fed by the info server's
rem alert feed. everything this role needs lives in the consumer
rem folder: config.yaml (role: consumer) · trades.db · consumer.log
rem this launcher lives in scripts\ - it cds into ..\consumer
cd /d %~dp0..\consumer

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
echo details in logs\consumer.log in the repo root ^(an external kill ^(exit 15^) leaves no traceback^)
ping -n 6 127.0.0.1 >nul
goto start

:end
