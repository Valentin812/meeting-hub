@echo off
setlocal
title Meeting Hub - Ollama setup
cd /d "%~dp0"
where ollama >/dev/null 2>nul
if errorlevel 1 (
  where winget >/dev/null 2>nul
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
