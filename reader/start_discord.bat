@echo off
set DISCORD_EXE=%LOCALAPPDATA%\Discord\Update.exe

if not exist "%DISCORD_EXE%" (
  echo Discord not found at %DISCORD_EXE%
  echo Edit this file to point at your Discord install.
  pause
  exit /b 1
)

tasklist /FI "IMAGENAME eq Discord.exe" 2>nul | find /I "Discord.exe" >nul
if %errorlevel%==0 (
  echo Discord is already running - leaving it alone.
  echo If the reader cannot see messages, Discord was started without
  echo the accessibility flag. Close Discord fully and rerun this script.
  exit /b 0
)

echo Starting Discord with accessibility enabled...
start "" "%DISCORD_EXE%" --processStart Discord.exe --process-start-args "--force-renderer-accessibility"
echo Done. UIA reader can now see the window.
