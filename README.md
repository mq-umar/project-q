# Project Q

Project Q is a personal AI executive assistant: a Windows-first local agent with an iPhone companion. This repository contains a runnable local MVP foundation that focuses on the core system described in the PRD:

- local orchestration
- provider-backed reasoning
- memory and task persistence
- agent registry
- routine registry and trusted automation runs
- immutable-style audit logging
- secure secret storage abstraction
- Playwright-backed browser automation
- Windows operator tools for app launch, window discovery, focus, and keystrokes
- desktop control surface
- iPhone companion code skeleton

## What Is Implemented

- Local Python API server using only the standard library plus `pydantic`
- SQLite-backed storage for conversations, memories, tasks, agents, routines, secrets, and audit events
- Windows DPAPI-backed vault service when running on Windows
- Policy engine with action tiers and approval enforcement
- Tool registry with filesystem, shell, browser, and Windows operator workers
- Remote model provider path via the OpenAI Responses API format
- Free local model provider path via Ollama-compatible chat endpoints
- Context assembly for tasks, memories, agents, routines, tools, and recent conversation
- Desktop UI for chat, tasks, memories, agents, routines, settings, and audit review
- SwiftUI companion scaffolding for the eventual iPhone app

## What Is Not Implemented Yet

- Live STT/TTS voice loop
- Richer Windows UI Automation and screen understanding
- Real connector integrations for mail, calendar, Slack, cloud drives, and Notes
- End-to-end encrypted phone relay
- Packaged MSIX and full iOS project wiring

## Run It

From PowerShell:

```powershell
.\launch_project_q.ps1
```

Or on Windows without PowerShell execution policy changes:

```bat
launch_project_q.bat
```

Then open [http://127.0.0.1:8787](http://127.0.0.1:8787).

## Configure Free Local Reasoning

Project Q now supports a free local model path through Ollama. Based on Ollama's official API docs, local requests can use `http://localhost:11434/api/chat` with no authentication, and local models can be listed from `/api/tags`. Sources: [Ollama API intro](https://docs.ollama.com/api), [chat endpoint](https://docs.ollama.com/api/chat), [list models](https://docs.ollama.com/api/tags)

1. Install Ollama for Windows.
2. Pull a local model:

```bat
setup_project_q_ollama.bat
```

3. In Project Q `Settings`, set:
   - `Enable advanced model provider`
   - `Provider type`: `ollama`
   - `Model name`: `qwen3.5:9b`
   - `Enable four-model routing`: on
   - `General model`: `qwen3.5:9b`
   - `Coding model`: `qwen2.5-coder:7b`
   - `Reasoning model`: `deepseek-r1:7b`
   - `Fast model`: `llama3.1:8b`
   - `Provider base URL`: `http://localhost:11434/api/chat`
   - `Secret name for API key`: leave blank
4. Save settings.
5. Use `Refresh Local Models` to confirm Project Q can see the installed model.

For your hardware, Project Q is now aligned to a verified four-model local stack:

```bat
setup_project_q_local_stack.bat
```

Use:
- `qwen3.5:9b` as the general planning/orchestration model
- `qwen2.5-coder:7b` as the coding model
- `deepseek-r1:7b` as the reasoning model
- `llama3.1:8b` as the fast-response model

If you only want the coding-focused subset first, use:

```bat
setup_project_q_coding_stack.bat
```

## Test The New Phase

After configuring Ollama and starting Project Q:

1. In chat, test local reasoning:
   - `What can you do right now for coding tasks?`
   - `What automations and agents can you run right now?`
2. Test memory and task creation:
   - `remember that this Project Q instance is optimized for software engineering`
   - `task: add Windows UI automation to Project Q`
3. Test agent creation:
   - `spawn a coding agent to review the current repo and plan the next implementation step`
4. In `Agent Registry`, click `Run Agent`
5. Test routine creation in chat:
   - `automation: open VS Code and list files in the workspace`
6. In `Routine Registry`, click `Run Routine`
7. In `Tool Runner`, test a safe repo operation:

```json
{
  "command": "Get-ChildItem",
  "workdir": ".",
  "timeout_seconds": 15
}
```

8. Test Windows operator tools:

```json
{
  "limit": 20
}
```

Use that with `windows.list_windows`, then try:

```json
{
  "command": "notepad.exe"
}
```

with `windows.launch_application`.

9. If browser tooling is configured, you can also test:

```json
{
  "url": "https://example.com",
  "include_links": true
}
```

## Configure API-Based Reasoning

1. Open `Settings` in the Project Q UI.
2. Store your API key in the `Vault` panel using the secret name you want to reference.
3. Set:
   - `Enable advanced model provider`
   - `Provider type`: `openai_responses`
   - `Model name`
   - `Provider base URL`
   - `Secret name for API key`
4. Save settings.

Project Q uses the OpenAI Responses API request format by default, which also works with compatible providers that expose the same endpoint shape.

## Enable Browser Automation

Project Q now includes Playwright-based browser tools:

- `browser.inspect_page`
- `browser.run_actions`

Before first use, install a Playwright browser or configure an existing browser path/channel in Settings.

Quick setup:

```bat
setup_project_q_browser.bat
```

If you prefer a local installed browser, set `browser_channel` or `browser_executable_path` in Settings.

## Manual Run

```powershell
$env:PYTHONPATH = "src"
C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m project_q.server
```

## Test

```powershell
$env:PYTHONPATH = "src"
C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s tests -v
```

## Repository Layout

- [src/project_q](/C:/Users/umarq.APEX/Documents/Jarvis/src/project_q)
- [docs/ARCHITECTURE.md](/C:/Users/umarq.APEX/Documents/Jarvis/docs/ARCHITECTURE.md)
- [ios/ProjectQCompanion](/C:/Users/umarq.APEX/Documents/Jarvis/ios/ProjectQCompanion)
