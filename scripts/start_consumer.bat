@echo off
rem consumer app: the trading pipeline fed by the info server's
rem alert feed. state lives apart from code:
rem   config\consumer.config.yaml - db\consumer.trades.db
rem this launcher lives in scripts\ - it cds into ..\consumer
cd /d "%~dp0..\consumer"

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


if not exist ..\config\consumer.config.yaml (
  echo config\consumer.config.yaml missing - copy ..\config\consumer.example.config.yaml there
  pause
  exit /b 1
)

:start
rem apply a staged release update (no-op without .update_pending_consumer.json)
..\.venv\Scripts\python.exe ..\scripts\apply_update.py ..
if errorlevel 1 (
  echo warning: apply_update failed - running the existing code
)
rem the db name (consumer.trades.db) is derived from the role in run.py
..\.venv\Scripts\python.exe ..\run.py -c ..\config\consumer.config.yaml
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
(echo consumer app exited with code %EXITCODE%)> ..\db\pipeline_exit_consumer.txt
echo details in logs\consumer.log in the repo root ^(an external kill ^(exit 15^) leaves no traceback^)
ping -n 6 127.0.0.1 >nul
goto start

:end
