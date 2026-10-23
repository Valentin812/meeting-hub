@echo off
setlocal
cd /d "%~dp0"
python app.py
echo.
echo Meeting Hub exited with code %errorlevel%.
pause
endlocal
