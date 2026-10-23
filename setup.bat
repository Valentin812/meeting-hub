@echo off
setlocal
title Meeting Hub Setup
cd /d "%~dp0"
echo Installing recording packages (numpy, PyAudioWPatch, soundcard)...
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
  echo Could not install recording packages. Meeting detection still works; recording will be unavailable.
  pause
)
where ollama >nul 2>nul
if errorlevel 1 (
  where winget >nul 2>nul
  if errorlevel 1 (
    echo Install Ollama from https://ollama.com/download/windows
    pause
    exit /b 1
  )
  winget install --id Ollama.Ollama -e
)
ollama pull qwen2.5:3b
if errorlevel 1 (
  echo Model setup failed. Check Ollama and internet.
  pause
  exit /b 1
)
echo Setup complete.
pause
endlocal
