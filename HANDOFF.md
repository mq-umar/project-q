# Project Q — Phase 1 Handoff Document

> **Verified:** 2026-06-08
> **Status:** Phase 1 complete; Phase 2 source and simulations complete with Apple release gates outstanding
> **Verification:** 188 application tests, 10 relay tests, Phase 1/2 verifiers, and 83 iOS source-contract checks pass

---

## Table of Contents

1. [How to Start the Server](#1-how-to-start-the-server)
2. [Phase 1 Completion Summary](#2-phase-1-completion-summary)
3. [Files Changed This Session](#3-files-changed-this-session)
4. [New Features: Usage Guide](#4-new-features-usage-guide)
5. [Architecture Overview](#5-architecture-overview)
6. [Known Limitations & Caveats](#6-known-limitations--caveats)
7. [Phase 2 Delivery](#7-phase-2-delivery)
8. [Quick Reference](#8-quick-reference)

---

## 1. How to Start the Server

```powershell
# Set PYTHONPATH and launch
$env:PYTHONPATH = "C:\Users\umarq.APEX\Documents\Jarvis\src"
$py = "C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
Set-Location "C:\Users\umarq.APEX\Documents\Jarvis"
& $py -m project_q.server
```

Server starts at **http://127.0.0.1:8787**

To verify everything is healthy after boot:
```powershell
& $py -X utf8 verify_phase1.py
```

---

## 2. Phase 1 Completion Summary

All 10 deliverables listed under PRD section 14, Phase 1 are implemented and
behaviorally verified:

| # | Capability | Implementation | Status |
|---|-----------|----------------|--------|
| 1 | Voice + text conversational loop | Native provider SSE/NDJSON, continuous/interim browser STT, streamed browser TTS, Windows SAPI fallback | DONE |
| 2 | Core memory (episodic + semantic) | SQLite persistence, FTS5 search, conversation context, retention controls | DONE |
| 3 | Web research | `research.web` inspects multiple domains, preserves citations/errors, and scans Zone 3 content | DONE |
| 4 | File system + browser automation | 11 bounded filesystem tools plus real-verified Playwright inspect/action/goal workers with visible isolated profiles | DONE |
| 5 | Terminal execution (sandboxed) | Docker with no network/resource limits/non-root/fail-closed; explicit `direct_trusted` host mode | DONE |
| 6 | Email + calendar read/draft | RFC822/ICS drafts plus opt-in Outlook desktop read/create/send paths | DONE |
| 7 | Basic task planning + tracking | CRUD, priority, status, due dates, context assembly, reflection hooks | DONE |
| 8 | Audit log | Append-only SQLite triggers, provenance, approval, model, outcome, and error fields | DONE |
| 9 | Secrets vault | Windows DPAPI `CryptProtectData`; names only exposed through APIs | DONE |
| 10 | Basic Research/Coding agents | Templates, bounded tools, execution loop, run history, and audit records | DONE |

**Tool inventory at Phase 1 complete:**
- `filesystem.*` — 11 tools
- `browser.*` — 3 tools
- `windows.*` — 17 tools
- `voice.*` — 2 tools
- `git.*` — 10 tools
- `outlook.*` — 5 tools
- `research.*` — 1 tool
- Other (shell, knowledge, code, agent, etc.) — ~16 tools
- **Total: 65 registered tools**

**Post-handoff audit fixes applied by Codex:**
- `verify_phase1.py` now uses a temporary database/data root and no longer mutates owner data during verification.
- Git tools now honor `settings.git_workspace` and reject path escapes outside the effective workspace.
- Outlook COM tools now require `outlook_enabled=true` and launch PowerShell with `-STA`.
- Existing memories are backfilled into the FTS5 index with an external-content `rebuild`.
- `scheduler_enabled=false` now prevents the scheduler from starting at app boot.
- Frontend HTML escaping now covers quotes for attribute contexts.
- HTTP route simulations now cover templates, template spawning, metrics, scheduler status, memory pruning, and SSE streaming.
- API JSON parsing now returns 400s for malformed/non-object payloads and rejects bodies over 2 MiB.
- Windows automation now includes `windows.app_state`, `windows.focus_follow`, and `windows.screenshot_diff`, bringing the Phase 1 Windows tool inventory to 17 registered tools.
- Browser runtime discovery now includes the bundled pnpm dependency tree, fixing the missing `playwright-core` launch failure.
- Browser actions now use a visible isolated persistent Edge profile by default, retain login state, support forms/tabs/downloads/owner pauses, and preserve model-generated interactive plans instead of rewriting them to URL-only opens.
- The Phase 1 verifier now launches the real browser and proves form interaction, tab control, downloads, screenshots, profile persistence, and private HTTP/WebSocket blocking; service workers are disabled in controlled contexts.

---

## 3. Files Changed This Session

### New Files

#### `src/project_q/tools/git_tools.py`
Ten git tools implemented via `subprocess` with a shared `_run_git()` helper.

| Tool ID | Class | Tier | Description |
|---------|-------|------|-------------|
| `git.status` | `GitStatusTool` | 0 | Working tree status |
| `git.init` | `GitInitTool` | 2 | Initialize repo |
| `git.add` | `GitAddTool` | 1 | Stage files |
| `git.commit` | `GitCommitTool` | 2 | Commit staged changes |
| `git.push` | `GitPushTool` | 3 | Push to remote |
| `git.pull` | `GitPullTool` | 2 | Pull from remote |
| `git.diff` | `GitDiffTool` | 0 | Show diff |
| `git.branch` | `GitBranchTool` | 2 | List/create/checkout/delete branch |
| `git.log` | `GitLogTool` | 0 | Commit history |
| `git.clone` | `GitCloneTool` | 2 | Clone repository |

All tools respect `settings.git_workspace` when configured, otherwise fall back to the application workspace root. Relative paths are resolved inside that effective root and path escapes are rejected. Timeout: 60s (120s for clone). Raise `RuntimeError(stderr)` on non-zero exit.

#### `src/project_q/tools/outlook.py`
Five Outlook COM tools via PowerShell `_run_powershell_json` helper. Uses integer constants (`olFolderInbox=6`, `olFolderCalendar=9`) to avoid assembly loading issues. All tools require `outlook_enabled=true`, use STA PowerShell for COM access, and emit `{error: "..."}` gracefully when Outlook is not installed.

| Tool ID | Class | Tier | Description |
|---------|-------|------|-------------|
| `outlook.email_list` | `OutlookEmailListTool` | 1 | List inbox emails |
| `outlook.email_read` | `OutlookEmailReadTool` | 1 | Read email body by EntryID |
| `outlook.email_send` | `OutlookEmailSendTool` | 3 | Send email via Outlook COM |
| `outlook.calendar_list` | `OutlookCalendarListTool` | 1 | List calendar events by date range |
| `outlook.calendar_create` | `OutlookCalendarCreateTool` | 2 | Create calendar appointment |

#### `src/project_q/services/scheduler.py`
`RoutineSchedulerService` — daemon thread that wakes every 60 seconds, checks all active `schedule`-type routines, and auto-runs any that are overdue. Interval is read from `metadata.schedule_interval_minutes` or `schedule_interval_minutes=N` in the routine's notes field.

```python
status = app.scheduler.status()
# → {running: bool, scheduled_count: int, next_runs: [...], last_checked: str}
```

---

### Modified Files

#### `src/project_q/models.py`
Added 6 new fields to `SettingsUpdate`:
```python
outlook_enabled: bool | None = None
scheduler_enabled: bool | None = None
auto_reflect_on_tasks: bool | None = None
metrics_enabled: bool | None = None
memory_retention_days: int | None = Field(default=None, ge=0, le=3650)
git_workspace: str | None = None
```
Also fixed a duplicate `memory_mode` field that previously caused silent drops.

#### `src/project_q/services/settings.py`
Added to `DEFAULT_SETTINGS`:
```python
"memory_retention_days": 90,
"git_workspace": "",
"scheduler_enabled": True,
"metrics_enabled": True,
"outlook_enabled": False,
"auto_reflect_on_tasks": False,
```

#### `src/project_q/services/agents.py`
- Added `BUILT_IN_TEMPLATES` module-level dict with 9 pre-built agent types
- Added `list_templates()` → returns list of `{id, name, goal, tools, ...}` dicts
- Added `spawn_from_template(template_id, goal_override="")` → creates an agent from a template

Built-in templates: `research`, `coding`, `testing`, `web_builder`, `monitor`, `writer`, `data`, `ops`, `git`

#### `src/project_q/services/learning.py`
- Added `get_metrics()` — computes from last 2000 audit entries + tasks + memories:
  - `task_completion_rate`, `tool_executions_24h`, `tool_failure_rate`
  - `agent_success_rate`, `top_tools[{tool_id, count, failure_rate}]`
  - `memory_count`, `tasks_completed_24h`, etc.
- Added `reflect_on_task(task_id)` — scans audit log for task activity, creates a `semantic` memory with a lesson learned
- Improved `_collect_findings()` — detects repeated tool failures (3+ threshold), low task completion, low agent success rates

#### `src/project_q/services/memory.py`
- Added `prune_expired()` — deletes `owner_confirmed=0` memories older than `memory_retention_days` setting (default 90). Returns `{pruned, retention_days, cutoff}`.
- Improved `search()` — tries FTS5 `memories_fts MATCH ?` first, falls back to `LIKE` with relevance scoring:
  - exact match → 1.0
  - starts-with → 0.8
  - contains → 0.6

#### `src/project_q/tools/registry.py`
Added conditional import blocks at end of `__init__` for git and outlook tools:
```python
try:
    from project_q.tools.git_tools import (GitStatusTool, ...)
    for tool in (...):
        self.tools[tool.definition.tool_id] = tool
except ImportError:
    pass  # gracefully degrade if git tools unavailable
```

#### `src/project_q/app.py`
- Added `scheduler: Any` field to `ProjectQApplication` dataclass
- `create_application()` starts `RoutineSchedulerService` after building `routine_runner` only when `scheduler_enabled=true`; wrapped in `try/except` so a scheduler failure never prevents app boot

#### `src/project_q/server.py`
Five new API routes added:

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| `GET` | `/api/agents/templates` | `_list_agent_templates` | List 9 built-in templates |
| `POST` | `/api/agents/from-template` | `_spawn_agent_from_template` | Create agent from template |
| `GET` | `/api/metrics` | `_get_metrics` | Learning metrics |
| `GET` | `/api/scheduler/status` | `_get_scheduler_status` | Scheduler thread status |
| `POST` | `/api/memories/prune` | `_prune_memories` | Prune expired memories |

`_update_task` now calls `learning.reflect_on_task(task_id)` when a task is completed and `auto_reflect_on_tasks=True` (best-effort, wrapped in try/except — never blocks the response).

#### `src/project_q/storage.py`
Added FTS5 virtual table and 3 sync triggers for fast memory search:
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
  USING fts5(text, content=memories, content_rowid=rowid);
-- Plus INSERT / UPDATE / DELETE triggers to keep FTS5 in sync
```
The schema migration also rebuilds the external-content FTS index so legacy memories become searchable.

#### `src/project_q/static/app.js` (1884 lines)
Major UI additions:

- **SSE Streaming chat** — `fetch /api/chat/stream`, reads `\n\n`-delimited SSE events, appends `evt.token` to a live `▌`-cursored message, then finalizes on `evt.done`
- **Markdown renderer** — `renderMarkdown()` + `processInline()`: fenced code blocks, headings, bullet/numbered lists, bold, italic, inline code — no external dependencies
- **Hero stats** — `statTools`, `statMemories`, `statTasks`, `statAgents` updated on every `render()` cycle
- **Agent templates panel** — `renderTemplates()` with emoji icons and one-click spawn buttons
- **Metrics panel** — `renderMetrics()` with 4 metric cards + top-tools ranked list
- **Memory bulk actions** — bulk-delete selected, import from JSON file, prune expired
- **Run history panels** — collapsible, fetched from `/api/agents/:id/runs` and `/api/routines/:id/runs`
- **Git / Outlook default payloads** in `defaultToolPayloads`
- **New settings fields** — `outlookEnabled`, `schedulerEnabled`, `autoReflectOnTasks`, `memoryRetentionDays`, `gitWorkspace`
- **Auto-refresh** — `setInterval(refreshAll, 30_000)` keeps all panels current
- **Conversation ordering** — `slice().reverse()` for oldest-first display; optimistic `unshift()` for user + Q messages during streaming

#### `src/project_q/static/index.html` (762 lines)
- Hero stats panel (4 `hero-stat` divs)
- Task priority select (`P0 Critical` → `P3 Low`)
- Memory card header: Import / Prune / Bulk Delete buttons + hidden file input
- Agent Templates section (`#section-templates`) with `.template-grid`
- Metrics section (`#section-metrics`) with `.metrics-grid` + top tools list
- Integrations settings group (Outlook, Scheduler, Auto-reflect, Retention, Git workspace)
- Nav links for Templates and Metrics

#### `src/project_q/static/styles.css` (1849 lines)
New CSS components:
- `.hero-stats`, `.hero-stat`, `.stat-value` (Orbitron glow), `.stat-label`
- `.md-p`, `.md-h1/2/3`, `.md-code`, `.md-inline-code`, `.md-list` (markdown styles)
- `.stream-cursor` with `blink-cursor` keyframe animation
- `.run-history-panel`, `.run-history-entry`
- `.card-header-actions` flex row
- `.template-grid`, `.template-card`, `.template-icon`, `.template-goal`
- `.metrics-grid`, `.metric-card`, `.metric-value`, `.metric-label`
- Outcome/tier tag colour variants

---

## 4. New Features: Usage Guide

### Git Tools
Enable by setting `git_workspace` in Settings to the root directory you want git operations scoped to. All `git.*` tools default to this workspace.

```
Settings → Behavior → Git Workspace: C:\dev\myproject
```

Then use via any agent goal, e.g.:
> "Commit all staged changes with message 'wip: progress on auth'"

Or invoke directly from the Tools panel.

### Outlook Integration
Must be enabled in Settings and requires Outlook desktop app installed:
```
Settings → Integrations → Outlook Integration: ON
```

Available actions:
- List recent inbox emails
- Read full email body by EntryID
- Send an email (Tier 3 — requires owner approval)
- List calendar events for a date range
- Create calendar appointments

### Agent Templates
Navigate to **Templates** in the sidebar. Nine ready-made agent types are available:

| Template | Purpose |
|----------|---------|
| `research` | Web research, source collection, summary |
| `coding` | Code generation, refactoring, debugging |
| `testing` | Test writing, coverage, regression checks |
| `web_builder` | Full-stack project scaffolding |
| `monitor` | System health checks, alerting |
| `writer` | Documents, emails, structured reports |
| `data` | Data analysis, transformation, stats |
| `ops` | Deployment, infra, system admin |
| `git` | Version control automation |

Click **Spawn** to create an agent from a template, optionally overriding the goal.

### Scheduled Routines
Create a routine with `trigger_type = "schedule"`. Set the interval by adding to the notes or metadata:
```
schedule_interval_minutes=60
```
The background scheduler checks every 60 seconds and auto-runs overdue routines. Monitor via:
```
GET /api/scheduler/status
```

### Metrics Dashboard
Navigate to **Metrics** in the sidebar. Shows:
- Task completion rate (24h)
- Tool executions + failure rate (24h)
- Agent success rate (24h)
- Top tools by usage with per-tool failure rate

Refreshes automatically every 30 seconds.

### Auto-Reflect on Tasks
When enabled, completing a task triggers `learning.reflect_on_task()`, which scans the audit log for activity tied to that task and creates a `semantic` memory with a lesson learned.

```
Settings → Behavior → Auto-reflect on completed tasks: ON
```

### Memory Management
Three bulk operations are available in the Memory card header:
- **Import** — pick a JSON file exported from a previous session
- **Prune** — delete expired unconfirmed memories (older than `memory_retention_days`)
- **Bulk Delete** — select memories with checkboxes, then delete selected

Memory search now uses FTS5 full-text indexing for fast, ranked results with LIKE fallback.

---

## 5. Architecture Overview

```
project_q/
├── app.py                  # ProjectQApplication dataclass + create_application()
├── server.py               # Single-file HTTP server (no framework), ~80 routes
├── storage.py              # SQLite schema + Database context manager
├── models.py               # Pydantic models for all domain objects
│
├── services/
│   ├── agents.py           # Agent CRUD + BUILT_IN_TEMPLATES + list_templates()
│   ├── learning.py         # get_metrics(), reflect_on_task(), run_learning_cycle()
│   ├── memory.py           # CRUD + FTS5 search + prune_expired()
│   ├── reasoner.py         # LLM routing → Anthropic Claude / local Ollama
│   ├── routines.py         # Routine CRUD + run()
│   ├── scheduler.py        # RoutineSchedulerService (daemon thread)        [NEW]
│   ├── settings.py         # DEFAULT_SETTINGS + get/set
│   ├── tasks.py            # Task CRUD + priority
│   └── vault.py            # Windows DPAPI secrets
│
├── tools/
│   ├── registry.py         # ToolRegistry — central registration
│   ├── base.py             # BaseTool, ToolDefinition, Tier enum
│   ├── filesystem.py       # 11 filesystem tools
│   ├── browser.py          # 3 Playwright tools
│   ├── windows_tools.py    # 17 Windows UI/OCR/focus/app-state/registry tools
│   ├── voice_tools.py      # 2 SAPI voice tools
│   ├── git_tools.py        # 10 git tools                                   [NEW]
│   └── outlook.py          # 5 Outlook COM tools                            [NEW]
│
└── static/
    ├── index.html          # Single-page app shell (762 lines)
    ├── app.js              # All frontend logic (1884 lines, no bundler)
    └── styles.css          # All styles (1849 lines)
```

### Request flow
```
Browser → HTTP GET/POST → server.py (route dispatch)
                        → service layer (agents/memory/tasks/learning/…)
                        → Database (SQLite via storage.py)
                        → tool execution (registry → individual tool)
                        → audit_log write (every Tier 1+ action)
```

### Streaming chat flow
```
Browser sends POST /api/chat/stream
  → ReasonerService.stream_plan() requests native provider streaming
  → OpenAI/Anthropic SSE or Ollama NDJSON yields structured-plan text deltas
  → Incremental JSON decoder exposes only the top-level `reply` string
  → Browser receives provider-sized token events and appends them live
  → Complete plan validates, then policy-controlled writes/tools execute
  → Canonical tool-result suffix and final `done` event close the stream
```

### Tier security model
| Tier | Examples | Approval |
|------|---------|----------|
| 0 | Read, inspect, status | Auto-approved |
| 1 | Write files, list emails | Auto-approved (logged) |
| 2 | Create agents, git commit, cal events | Requires `trusted=true` or owner session |
| 3 | Send email, git push, secrets write | Always requires owner approval |

---

## 6. Known Limitations & Caveats

1. **Voice requires an active dashboard** — Edge/Chrome continuous recognition and streamed speech synthesis work while the dashboard is open. Wake-word activation and background listening remain later-phase work.

2. **Outlook requires desktop Outlook** — Outlook COM tools use `New-Object -ComObject Outlook.Application`. Requires Outlook (classic, not New Outlook) installed. Microsoft 365 web-only users will see `{error: "Outlook COM not available"}` on all 5 tools.

3. **Structured execution waits for validation** — OpenAI, Anthropic, and Ollama reply text now streams from their native protocols, but Project Q intentionally buffers the complete structured plan before any memory/task/agent/routine write or tool execution.

4. **Scheduler syntax is interval-only** — Routine run history is persisted in `routine_runs` and survives restarts, but scheduled routines currently use `schedule_interval_minutes` rather than cron or wall-clock calendar expressions.

5. **Memory import format** — The import feature expects a JSON array of memory objects matching the `MemoryCreate` schema. Malformed files are rejected by the API, but UI feedback is still minimal.

6. **Git workspace default** — Git tools are confined to `settings.git_workspace` when set. If this setting is empty, git tools default to the server's workspace root, so set it explicitly for production use.

7. **Windows SAPI fallback** — `voice.speak` and `voice.listen_once` remain synchronous compatibility tools. The primary dashboard path uses browser streaming speech.

8. **Docker sandbox prerequisite** — `sandbox_first` fails closed unless Docker Desktop is running and the PowerShell sandbox image is available. Host execution requires explicitly selecting `direct_trusted`.

---

## 7. Phase 2 Delivery

The Phase 2 PRD surface is implemented in source and behaviorally simulated:

- expiring QR/copy-link pairing with X25519/HKDF and no displayed shared key
- replay-safe direct companion authentication and AES-256-GCM E2E envelopes
- self-hostable FastAPI relay with ciphertext-only routing, scoped tokens,
  presence, revocation, WebSocket/poll delivery, and APNs wake support
- native SwiftUI app, widget, share extension, App Intents, BackgroundTasks,
  encrypted cache/offline queue, and shared Keychain access group
- direct token-streaming chat with encrypted-relay fallback and session history
- biometric approvals, pending/completed history, task/agent activity, routines,
  quick text/voice/URL capture, memory editing, redacted audit review, and kill switch

Verification evidence:

- `188` application unit/integration tests pass
- `10` relay tests pass
- `verify_phase1.py`, `verify_phase2_backend.py`, and
  `verify_phase2_relay.py` pass
- `scripts/audit_phase2_ios.py` passes `83` checks
- Python compilation, JavaScript parsing, Docker Compose validation, and
  `git diff --check` pass

External release gates:

- generate and build the Xcode project on macOS
- configure Apple signing and execute the native unit tests
- verify camera pairing, Face ID/Touch ID, widget/share/background behavior,
  and offline recovery on a physical device
- configure owner APNs credentials and verify live sandbox/production delivery

The next PRD work is Phase 3: dependency-aware parallel orchestration, versioned
agent definitions and budgets, iterative build/test/fix pipelines, routine
rollback, procedural playbooks, broader connectors, wake word, and per-app
screen-context consent.

---

## 8. Quick Reference

### API Endpoints (new in this session)

```
GET  /api/agents/templates          → list 9 built-in templates
POST /api/agents/from-template      → {template_id, goal_override?}
GET  /api/metrics                   → learning metrics
GET  /api/scheduler/status          → {running, scheduled_count, next_runs}
POST /api/memories/prune            → prune expired memories
```

### Key Settings

| Key | Default | Description |
|-----|---------|-------------|
| `memory_retention_days` | 90 | Days to retain unconfirmed memories |
| `scheduler_enabled` | `true` | Enable background routine scheduler |
| `outlook_enabled` | `false` | Enable Outlook COM integration |
| `auto_reflect_on_tasks` | `false` | Auto-create memory on task completion |
| `metrics_enabled` | `true` | Enable learning metrics collection |
| `git_workspace` | `""` | Root path for git tool operations |
| `memory_mode` | `"persistent"` | `"persistent"` or `"ephemeral"` |
| `proactive_mode` | `false` | Enable proactive suggestions |
| `screen_context` | `false` | Enable screen capture context |
| `network_policy` | `"full"` | `"full"`, `"local"`, or `"offline"` |

### Database Tables

| Table | Purpose |
|-------|---------|
| `conversations` | Chat history |
| `memories` | Episodic + semantic memories |
| `memories_fts` | FTS5 virtual table for fast search |
| `tasks` | Task tracking |
| `agents` | Agent definitions |
| `agent_runs` | Agent execution history |
| `routines` | Scheduled/trigger routines |
| `routine_runs` | Routine execution history |
| `audit_log` | Immutable action log |
| `settings` | Key-value settings store |
| `secrets` | DPAPI-encrypted secrets |
| `learning_runs` | Learning cycle results |
| `diagnostic_runs` | Diagnostic results |
| `project_dispatches` | Project execution records |
| `companion_devices` | Paired mobile/remote devices |
| `companion_envelopes` | Encrypted message relay |
| `owner_sessions` | Remote auth sessions |

### Python Path

```
interpreter: C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
PYTHONPATH:  C:\Users\umarq.APEX\Documents\Jarvis\src
project_root: C:\Users\umarq.APEX\Documents\Jarvis
```

---

*Phase 1 complete for the PRD checklist: 10/10 deliverables behaviorally verified, 65 tools registered, 9 agent templates, append-only audit enforcement, FTS5 memory search, bounded multi-source research, streaming dashboard voice, and fail-closed sandbox selection.*
