@echo off
setlocal

where ollama >nul 2>nul
if errorlevel 1 (
  echo Ollama is not installed.
  echo Download it for Windows from:
  echo https://ollama.com/download/windows
  exit /b 1
)

echo Pulling the Project Q coding-focused local stack...
echo 1/3 qwen3.5:9b
ollama pull qwen3.5:9b

echo.
echo 2/3 qwen2.5-coder:7b
ollama pull qwen2.5-coder:7b

echo.
echo 3/3 deepseek-r1:7b
ollama pull deepseek-r1:7b

echo.
echo Recommended defaults for Project Q:
echo Provider type: ollama
echo Model name: qwen3.5:9b
echo Provider base URL: http://localhost:11434/api/chat
echo Enable four-model routing: on
echo General model: qwen3.5:9b
echo Coding model: qwen2.5-coder:7b
echo Reasoning model: deepseek-r1:7b
echo Fast model: leave blank or install llama3.1:8b later
echo Leave Secret name blank
echo.
echo This setup is optimized for coding-first use on your current hardware.
