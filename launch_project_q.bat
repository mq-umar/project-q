@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$runtimePath = Join-Path '%~dp0' '.project_q\runtime.json'; if (Test-Path $runtimePath) { try { $runtime = Get-Content $runtimePath -Raw | ConvertFrom-Json; $existingPid = [int]($runtime.pid); if ($existingPid -gt 0) { $existingProcess = Get-CimInstance Win32_Process -Filter \"ProcessId = $existingPid\" -ErrorAction SilentlyContinue; if ($existingProcess -and $existingProcess.CommandLine -match 'project_q\.server') { Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 800 } } } catch { }; Remove-Item $runtimePath -Force -ErrorAction SilentlyContinue }"
set PYTHONPATH=src
"C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m project_q.server
