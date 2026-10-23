@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
python audio_check.py
start "" notepad meeting_hub_audio_report.txt
pause
endlocal
