@echo off
setlocal
title Meeting Hub Setup
cd /d "%~dp0"
rem Meeting Hub v5 needs no extra Python packages: meeting detection uses only Windows built-ins.
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
