@echo off
setlocal

where ollama >nul 2>nul
if errorlevel 1 (
  echo Ollama is not installed.
  echo Download it for Windows from:
  echo https://ollama.com/download/windows
  exit /b 1
)

echo Pulling the main coding model for Project Q...
ollama pull qwen3.5:9b

echo.
echo If the pull completed, open Project Q and use:
echo Provider type: ollama
echo Model name: qwen3.5:9b
echo Provider base URL: http://localhost:11434/api/chat
echo Leave Secret name blank for local Ollama use.
