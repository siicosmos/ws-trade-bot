@echo off
set DISCORD_EXE=%LOCALAPPDATA%\Discord\Update.exe

if not exist "%DISCORD_EXE%" (
  echo Discord not found at %DISCORD_EXE%
  echo Edit this file to point at your Discord install.
  pause
  exit /b 1
)

echo Closing running Discord instances...
taskkill /IM Discord.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul

echo Starting Discord with accessibility enabled...
start "" "%DISCORD_EXE%" --processStart Discord.exe --process-start-args "--force-renderer-accessibility"

echo Done. UIA reader can now see the window.
