@echo off
setlocal

where ollama >nul 2>nul
if errorlevel 1 (
  echo Ollama is not installed.
  echo Download it for Windows from:
  echo https://ollama.com/download/windows
  exit /b 1
)

echo Pulling the verified Project Q four-model local stack...
echo 1/4 qwen3.5:9b
ollama pull qwen3.5:9b

echo.
echo 2/4 qwen2.5-coder:7b
ollama pull qwen2.5-coder:7b

echo.
echo 3/4 deepseek-r1:7b
ollama pull deepseek-r1:7b

echo.
echo 4/4 llama3.1:8b
ollama pull llama3.1:8b

echo.
echo Recommended Project Q settings:
echo Provider type: ollama
echo Enable advanced model provider: on
echo Enable four-model routing: on
echo Primary/default model: qwen3.5:9b
echo General model: qwen3.5:9b
echo Coding model: qwen2.5-coder:7b
echo Reasoning model: deepseek-r1:7b
echo Fast model: llama3.1:8b
echo Provider base URL: http://localhost:11434/api/chat
echo Secret name: leave blank
