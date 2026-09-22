@echo off
rem Clean start: wipe recorded signals, the trade log and log files.
rem Paper ledgers and config are kept. Stop the pipeline and
rem reader before running this.
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo No .venv found - run from the repo root with python.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "scripts\clean_start.py" %*
echo.
pause
