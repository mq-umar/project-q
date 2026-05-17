@echo off
setlocal
set NODE_EXE=C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe
set NODE_MODULES=C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules
set PLAYWRIGHT_CLI=%NODE_MODULES%\playwright\cli.js

if not exist "%PLAYWRIGHT_CLI%" (
  echo Playwright CLI was not found at:
  echo %PLAYWRIGHT_CLI%
  exit /b 1
)

set NODE_PATH=%NODE_MODULES%
"%NODE_EXE%" "%PLAYWRIGHT_CLI%" install chromium

