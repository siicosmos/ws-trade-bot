@echo off
rem info server: reader ingest + alert feed, no trading.
rem everything this role needs lives in the info folder:
rem   info.config.yaml · info.trades.db · info.log
rem this launcher lives in scripts\ - it cds into ..\info
cd /d %~dp0..\info

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


if not exist ..\config\info.config.yaml (
  echo config\info.config.yaml missing - copy ..\config\info.example.config.yaml there
  pause
  exit /b 1
)

:start
rem the db name (info.trades.db) is derived from the role in run.py
..\.venv\Scripts\python.exe ..\run.py -c ..\config\info.config.yaml
set EXITCODE=%errorlevel%
if %EXITCODE% == 0 (
  echo info server stopped cleanly
  goto end
)
if %EXITCODE% == -1073741510 (
  echo info server stopped via ctrl+c
  goto end
)
echo info server exited with code %EXITCODE% - restarting in 5s
(echo info server exited with code %EXITCODE%)> ..\db\pipeline_exit_info.txt
echo details in logs\info.log in the repo root ^(an external kill ^(exit 15^) leaves no traceback^)
ping -n 6 127.0.0.1 >nul
goto start

:end
