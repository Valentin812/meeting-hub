@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
  echo pythonw.exe was not found. Run run_meeting_hub_debug.bat for diagnostics.
  pause
  exit /b 1
)
start "" pythonw app.py
endlocal
