# Project Q Architecture

## MVP Shape

The initial build is intentionally Windows-first and local-first.

### Core layers

1. `Desktop UI`
   A static single-page application served by the local backend. It provides the conversational shell, task queue, memory browser, agent monitor, audit viewer, and settings panel.
2. `Application Server`
   A lightweight Python HTTP server that exposes JSON endpoints, assembles context, routes chat interactions, enforces owner sessions and policy, exposes emergency control endpoints, and logs all consequential actions.
3. `Service Layer`
   Focused services handle conversations, context assembly, reasoning, plan execution, multi-source research, agent runs, routine runs, memories, tasks, agents, routines, settings, owner sessions, emergency control, companion pairing/envelopes, trust-boundary scanning, voice transcript ingestion, vault access, and audit events.
4. `Tool Workers`
   Tool workers include filesystem read/write/search/move/zip/watch operations, Docker-sandboxed or explicitly trusted-direct shell execution, Playwright browser primitives with isolated persistent profiles and in-worker HTTP/WebSocket private-network guards, multi-source research, local productivity draft artifacts, security scanners, local text-to-speech output, and Windows operator primitives for app launch, notifications, open-window discovery, UI Automation tree inspection and invocation, scoped registry access, window activation, keystrokes, clipboard access, screenshot capture, and OCR extraction from screenshot artifacts.
5. `Persistence`
   SQLite stores operational state. Secrets are stored separately and encrypted via DPAPI on Windows.

## Security Model

- Consequential actions are recorded in an append-only audit table protected by SQLite update/delete rejection triggers.
- Tools are assigned an action tier.
- Tier 0 and Tier 1 tools may run automatically depending on settings.
- Tier 2 and Tier 3 tools require explicit approval unless the owner later defines a trusted routine.
- The emergency kill switch blocks new mutating API calls and tool authorizations until the owner resumes Project Q.
- Companion devices pair through an expiring QR/deep-link offer, perform X25519 key agreement, derive direction-separated keys with HKDF-SHA256, sign direct requests with replay-safe HMAC authentication, and exchange AES-256-GCM envelopes through direct or relay transport.
- Approval payloads are frozen under AES-256-GCM on Windows. Tier 3 decisions require a biometric-backed P-256 signature from the paired iPhone and execute at most once.
- The relay stores only opaque envelopes, hashes bearer tokens, encrypts APNs device tokens at rest, rejects HTTP credential redirects, and supports immediate device revocation.
- Mutating dashboard/API routes require a local owner session cookie or owner-session header. Emergency stop and signed companion quick-capture have separate narrowly scoped auth paths.
- External web/email/document content is labeled as `zone_3_external`, scanned for instruction-injection patterns, wrapped as untrusted data, and blocked from acting as owner approval for privileged tools.
- Voice transcripts enter through owner-session-protected APIs or local services, are audited with voice provenance, and run through the same conversation executor and tool policy as typed chat.
- Secrets never appear in logs, prompts, or memory records.

## Reasoning Layer

- `ContextService` assembles recent messages, task state, memories, agents, and available tools.
- `ContextService` now also includes routines so the model can build on reusable automations.
- `ReasonerService` supports built-in heuristic planning, free local-model planning through Ollama-compatible chat endpoints, and OpenAI-compatible remote planning.
- `ReasonerService` also supports Anthropic's Messages API through a Vault-stored API key, keeping provider credentials out of prompts, logs, and memory.
- Streaming chat consumes native OpenAI Responses SSE, Anthropic Messages SSE, or Ollama NDJSON. Only the decoded top-level plan `reply` reaches the UI incrementally; the complete structured plan must validate before any write or tool execution.
- Local Ollama model discovery is supported through the `/api/tags` endpoint.
- The local-first stack now supports a routed four-model Ollama setup: `qwen3.5:9b` for general work, `qwen2.5-coder:7b` for coding, `deepseek-r1:7b` for reasoning, and `llama3.1:8b` for fast turns.
- Conversation turns can now create tasks, memories, agents, routines, and auto-execute allowed tools in a single pass.

## Memory Controls

- `MemoryService.export_all` returns a versioned, provenance-preserving JSON export of local memories.
- `MemoryService.bulk_delete` supports dry-run preview and scoped deletion by source, kind, tag, age, or explicit all-memory confirmation.
- `/api/memories/export` and `/api/memories/bulk-delete` require a local owner session and write audit entries.

## Productivity Drafts

- `communications.email_draft` creates local `.eml` draft artifacts for owner review; it rejects header injection and does not send mail.
- `calendar.create_invite` creates local `.ics` invite artifacts for owner review; external calendar sync remains a future connector layer.
- `PlanExecutorService` applies a reasoner plan consistently across conversations and agent runs.
- `AgentRunnerService` gives agents a first execution loop with run history, audit logging, and latest-run summaries.
- `RoutineRunnerService` gives trusted routines a reusable execution path with step-level results, approval gating, and run history.
- `RoutineSchedulerService` derives `last_run_at` from persisted `routine_runs`, so interval schedules survive process restarts without a separate in-memory timestamp.

## Desktop UI Modules

- `Chat`
- `Tasks`
- `Memories`
- `Agents`
- `Routines`
- `Audit`
- `Emergency Control`
- `Companion Pairing`
- `Settings`

## Voice Layer

- Edge/Chrome dashboard clients use continuous recognition with interim transcripts and feed final text into the normal composer.
- Native provider reply deltas are buffered into sentence-sized `SpeechSynthesisUtterance` chunks. Starting microphone capture cancels active speech for interruption handling.
- `voice.speak` uses local Windows text-to-speech when `voice_enabled` is on.
- `voice.listen_once` captures one local microphone utterance through Windows speech recognition when `voice_enabled` is on.
- `VoiceService` ingests already-transcribed text, records audit provenance, and routes the transcript through the same conversation path as chat.
- Wake-word detection and background listening outside the active dashboard are not yet implemented.

## Research Layer

- `research.web` converts a query or seed URL list into a bounded set of domain-diverse public sources.
- Every source is inspected through the Playwright worker and scanned as `zone_3_external` before it is included in the result.
- Results preserve title, URL, excerpt, trust findings, and per-source errors. A deterministic cited digest remains available when no model provider is configured.

## Shell Isolation

- `sandbox_first` invokes a PowerShell Docker image with networking disabled, memory/CPU/PID limits, dropped Linux capabilities, `no-new-privileges`, and a non-root user. Only the approved working directory is mounted.
- If Docker is unavailable, sandbox mode fails closed.
- `direct_trusted` is the explicit host PowerShell mode. It remains Tier 2, respects approved roots, and reports `sandboxed: false`.

## Windows Automation Layer

- `windows.app_state` reports bounded process/window state by process id, process name, or window title.
- `windows.focus_follow` finds the best matching visible window by heuristic and activates it.
- `windows.inspect_ui_tree` exposes bounded Windows UI Automation metadata for a target window.
- `windows.invoke_ui_element` can invoke UIA elements by automation id, name, or control type after owner approval.
- `windows.registry_read` reads scoped software/environment registry values; `windows.registry_write` is Tier 3 and limited to the Project Q HKCU scope.
- `windows.notify` dispatches a local Windows toast when notifications are enabled.
- `windows.ocr_screenshot` extracts text from screenshot artifacts under `.project_q/windows_artifacts`.
- `windows.screenshot_diff` compares two screenshot artifacts under `.project_q/windows_artifacts` with bounded pixel sampling.
- Continuous screen monitoring and richer visual state reasoning are not yet implemented.

## File Watching

- `filesystem.write_file` creates a timestamped snapshot under `.project_q/file_snapshots` before overwriting an existing file.
- `filesystem.watch_start` records a JSON snapshot baseline under `.project_q/file_watches`.
- `filesystem.watch_poll` compares the baseline with current files and reports added, modified, and deleted paths.
- This is polling-based rather than a long-running OS event subscription, which keeps it simple and auditable for the current local runtime.

## iPhone Companion

The native SwiftUI companion is defined by `ios/ProjectQCompanion/project.yml`
and includes app, widget, share-extension, and test targets. Its source covers:

- QR scanning and copy/paste pairing without exposing a shared symmetric key
- direct authenticated sync plus E2E encrypted relay fallback and offline queueing
- provider-token streaming chat, push-to-talk, synchronized conversation history, and speech output
- biometric approval signatures, pending/completed approval history, and execution outcomes
- task/agent activity, routine triggers and recent outcomes, quick text/voice/URL capture, memory search/edit/delete, and a redacted audit viewer
- APNs registration and generic wake notifications, App Intents/App Shortcuts, widget status, share extension, encrypted cache, and bounded background refresh
- biometric gates for memory and audit views and an always-available emergency stop

The Windows environment cannot perform the final Xcode build, signing,
simulator, real-device biometric, or live APNs credential checks. Those remain
release gates rather than missing source features.

## Next Build Priorities

1. Add richer visual state reasoning and broader UI Automation workflows
2. Add wake-word handling and background listening outside the dashboard
3. Validate the generated iOS project, signing, real-device biometrics, and live APNs delivery on macOS/Xcode
4. Add real connector integrations
5. Expand agent orchestration into dependency-aware parallel work
