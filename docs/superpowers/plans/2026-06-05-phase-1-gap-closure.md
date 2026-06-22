# Project Q Phase 1 Gap Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checked checkbox syntax for tracking.

**Goal:** Close the streaming voice, multi-source research, and true shell isolation gaps in the Project Q Phase 1 Windows MVP.

**Architecture:** Add one bounded research service/tool, replace ambiguous shell execution with explicit container/direct backends, and enhance the existing web client with native continuous speech recognition and streamed speech synthesis. Preserve the current local HTTP server, policy engine, tool registry, and SSE conversation flow.

**Tech Stack:** Python 3.12, Pydantic, SQLite, Playwright worker, Docker CLI, browser Web Speech APIs, vanilla JavaScript, `unittest`.

---

### Task 1: Multi-source research service

**Files:**
- Create: `src/project_q/services/research.py`
- Create: `src/project_q/tools/research.py`
- Modify: `src/project_q/app.py`
- Modify: `src/project_q/services/reasoner.py`
- Test: `tests/test_project_q.py`

- [x] **Step 1: Write failing service tests**

Add tests proving the service deduplicates domains, scans all retrieved text as
Zone 3 content, preserves partial failures, and returns at least two successful
sources plus a deterministic synthesis.

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_project_q.ProjectQApplicationTests.test_web_research_collects_diverse_scanned_sources
```

Expected: import or tool lookup failure because `research.web` does not exist.

- [x] **Step 3: Implement the bounded research service and tool**

`WebResearchService.research()` accepts `query`, `seed_urls`, and
`max_sources`. It uses the injected browser inspector, filters HTTP(S) links,
prefers distinct domains, scans excerpts with `TrustBoundaryService`, and
returns `query`, `summary`, `sources`, `source_count`, and `errors`.

- [x] **Step 4: Register and route the tool**

Register `ResearchWebTool` in `create_application()`. Add deterministic
reasoner routing for explicit research requests without URLs while retaining
single-page inspection for URL-specific requests.

- [x] **Step 5: Run research tests and verify GREEN**

Run the focused service, routing, and conversation tests. Expected: PASS.

### Task 2: Explicit shell execution backends

**Files:**
- Modify: `src/project_q/tools/shell.py`
- Modify: `src/project_q/services/settings.py`
- Modify: `src/project_q/models.py`
- Modify: `src/project_q/static/index.html`
- Modify: `src/project_q/static/app.js`
- Test: `tests/test_project_q.py`

- [x] **Step 1: Write failing sandbox tests**

Add tests that require `sandbox_first` to construct a Docker invocation with:

```text
--network none
--memory 512m
--cpus 1
--cap-drop ALL
--security-opt no-new-privileges
--user 65534:65534
-v <approved-path>:/workspace
```

Also assert that unavailable Docker fails closed and that `direct_trusted`
reports `sandboxed: false`.

- [x] **Step 2: Run focused shell tests and verify RED**

Expected: current tool invokes PowerShell directly and lacks backend metadata.

- [x] **Step 3: Implement Docker and trusted-direct backends**

Select backend from the validated `execution_environment` setting. Container
mode runs a pinned PowerShell image with network and privilege restrictions.
Direct mode keeps approved-root resolution and Tier 2 policy behavior.

- [x] **Step 4: Expose the execution-mode setting clearly**

Use a select control with `Sandbox first` and `Trusted direct` options. Do not
label trusted direct execution as sandboxed anywhere in the UI or docs.

- [x] **Step 5: Run shell tests and verify GREEN**

Run all shell, settings, and policy tests. Expected: PASS.

### Task 3: Streaming voice client

**Files:**
- Modify: `src/project_q/static/index.html`
- Modify: `src/project_q/static/app.js`
- Modify: `src/project_q/static/styles.css`
- Modify: `src/project_q/services/voice.py`
- Test: `tests/test_project_q.py`

- [x] **Step 1: Write failing static/runtime tests**

Add assertions for `SpeechRecognition`/`webkitSpeechRecognition`, continuous
mode, interim results, `speechSynthesis.cancel()`, sentence-sized response
speech, and the existing `voice.listen_once` fallback.

- [x] **Step 2: Run tests and verify RED**

Expected: static assertions fail because the client only calls
`voice.listen_once`.

- [x] **Step 3: Implement continuous recognition**

Clicking the microphone starts/stops native recognition. Interim text updates
the composer without submission. Final text remains editable. Starting capture
cancels spoken output. Unsupported browsers call the SAPI one-shot fallback.

- [x] **Step 4: Implement streamed response speech**

Feed SSE reply deltas to a sentence buffer. Queue complete phrases through
`SpeechSynthesisUtterance`; flush remaining text on the done event. Keep text
chat functional when voice is disabled or synthesis is unavailable.

- [x] **Step 5: Run voice and static tests and verify GREEN**

Run focused Python tests and `node --check`. Expected: PASS.

### Task 4: Behavioral Phase 1 verifier

**Files:**
- Modify: `verify_phase1.py`
- Modify: `README.md`
- Modify: `HANDOFF.md`
- Modify: `docs/ARCHITECTURE.md`

- [x] **Step 1: Replace tool-count claims with behavioral checks**

The verifier must create/search episodic and semantic memories, execute a
multi-source research simulation, verify shell backend selection, exercise
email/calendar drafts, create/run Research and Coding agents, inspect audit
provenance, and prove vault ciphertext does not contain plaintext.

- [x] **Step 2: Correct documentation**

Describe browser-native streaming speech plus SAPI fallback, bounded
multi-source research, and Docker sandbox prerequisites. Remove the false
claims that working-directory scoping is isolation or that a browser goal tool
alone is multi-source research.

- [x] **Step 3: Run the isolated verifier**

Run:

```powershell
$env:PYTHONPATH='src'
python verify_phase1.py
```

Expected: every PRD Phase 1 requirement reports PASS, with Docker availability
reported separately from backend-contract verification.

### Task 5: Full verification and simulations

**Files:**
- Modify if needed: `tests/test_project_q.py`
- Modify if needed: `docs/SECURITY_AUDIT_2026-05-27.md`

- [x] **Step 1: Run focused security simulations**

Verify research prompt-injection text cannot authorize Tier 2 tools, secrets do
not appear in research/voice/audit payloads, and sandbox mode cannot silently
fall back to host execution.

- [x] **Step 2: Run the complete suite**

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_project_q
python -m compileall -q src scripts
node --check src/project_q/static/app.js
git diff --check
```

Expected: all commands exit zero.

- [x] **Step 3: Run local HTTP simulations**

Start an ephemeral Project Q server and exercise owner authentication, local
chat, provider-stream chat with a fake transport, voice transcript ingestion,
research execution, agent runs, task/memory persistence, and kill-switch
blocking.

- [x] **Step 4: Record exact evidence**

Update the Phase 1 checklist with exact test counts, simulation outcomes,
environment prerequisites, and remaining post-Phase-1 work. Do not call Phase
1 complete if any required behavior is only a registration/count assertion.

## Completion Evidence

- `python -m unittest tests.test_project_q`: 159 tests passed.
- `python verify_phase1.py`: all eight behavioral verification sections passed.
- `python -m compileall -q src scripts verify_phase1.py`: passed.
- `node --check` for the dashboard and Playwright worker: passed.
- Desktop and 390px mobile browser smoke tests: no console errors, horizontal
  overflow, or voice/send control overlap.
- `git diff --check`: passed with line-ending warnings only.
- Docker Desktop must be running for `sandbox_first`; it fails closed when the
  daemon or sandbox image is unavailable.
