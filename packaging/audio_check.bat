@echo off
setlocal
cd /d "%~dp0"
echo Checking sound devices, about 20 seconds...
start "" /wait MeetingHub.exe --audio-check
start "" notepad meeting_hub_audio_report.txt
endlocal
