# Meeting Hub local AI bootstrap
# Installs the official Ollama Windows runtime if needed and downloads the small local model.
$ErrorActionPreference = "Stop"
$model = "qwen2.5:1.5b-instruct"

function Find-Ollama {
  $paths = @(
    "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
    "$env:LOCALAPPDATA\Ollama\ollama.exe",
    "$env:ProgramFiles\Ollama\ollama.exe"
  )
  foreach ($p in $paths) { if (Test-Path $p) { return $p } }
  $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

$ollama = Find-Ollama
if (-not $ollama) {
  irm https://ollama.com/install.ps1 | iex
  Start-Sleep -Seconds 5
  $ollama = Find-Ollama
}
if (-not $ollama) { throw "Ollama installation failed" }

& $ollama pull $model
