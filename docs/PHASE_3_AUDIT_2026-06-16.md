# Project Q Phase 3 Audit

Verified on: 2026-06-21
Branch: `codex/phase3-orchestration`
Worktree: `C:\Users\umarq.APEX\Documents\Jarvis\.worktrees\phase3-orchestration`

## Summary

Phase 3 now includes durable workflow orchestration, versioned agent definitions, versioned routine definitions with rollback, human approval checkpoints, subprocess worker isolation, guarded LoRA evaluation/promotion, dashboard/API coverage, and atomic workflow transition event/checkpoint outbox behavior.

The implemented Phase 3 surfaces passed the focused workflow suites and full Phase 3 suite after the latest atomicity work. The full main Project Q suite must still be reinvestigated because the latest post-atomicity run timed out after about 604 seconds. A previous pre-atomicity main run passed 201 tests in 398.874 seconds, but that is not enough to claim current full-suite green.

The first real LoRA job, `lora_job_e5db0a2a9c95`, is valid and promising but remains inactive. It scored 10/12 on the clean holdout comparison, below the configured 0.85 promotion floor by one prompt, so Project Q correctly records it as not promoted.

## Verification Evidence

Current post-atomicity evidence:

- `python -m compileall -q src/project_q/services/workflow_service.py`: passed.
- Workflow atomic rollback regression subset: 7 tests passed in 4.253 seconds.
- Focused workflow suite: 67 tests passed in 47.876 seconds.
- Full Phase 3 suite: 117 tests passed in 112.007 seconds.
- Full main suite after latest atomicity patch: timed out after about 604 seconds. Investigate before claiming current green.

Previous evidence before latest atomicity patch:

- `python -m compileall -q src`: passed.
- `node --check src/project_q/static/app.js`: passed.
- Phase 3 suite: 110 tests passed in 83.286 seconds.
- Main application suite: 201 tests passed in 398.874 seconds.
- `verify_phase1.py`: passed, including the real browser worker check.
- `verify_phase2_backend.py`: passed.
- `verify_phase2_relay.py`: not run to completion in this bundled Python runtime because `fastapi` is not installed.
- Live HTTP workflow simulation: completed workflow reached `completed` with two succeeded delay nodes; cancellation workflow reached `cancelled` with its live node cancelled.
- Project Q browser engine self-test: all checks passed: launch, form, tabs, upload, download, screenshot, persistence, private HTTP blocking, private WebSocket blocking.
- Browser screenshot artifact verified visually: `.browser_engine_qa/browser_artifacts/browser-self-test.png`.

## Implemented Phase 3 Work

- Added SQLite WAL, busy timeout, foreign keys, rerunnable migrations, and concurrent-write coverage.
- Added immutable agent definition versions and exact agent run snapshots with parent/root/depth metadata.
- Enforced agent budgets and success contracts, including max tool calls, tool whitelists, required reply terms, required tool IDs, required artifacts, minimum score, and time budget.
- Blocked draft and blocked agents from starting new runs.
- Added durable workflow definitions, immutable versions, run snapshots, node runs, ordered events, checkpoints, retries, cancellation, timeouts, and restart reconciliation.
- Added approval workflow nodes that wait for explicit owner decisions and do not spawn worker subprocesses.
- Changed workflow retry so prior approval decisions are never replayed into a new run.
- Added subprocess node workers with bounded logs, custom data root/db path propagation, worker mode, timeout handling, and cancellation.
- Hardened worker launch failure cleanup so a child is cancelled and closed if PID persistence fails.
- Hardened Windows process handling with `taskkill.exe /T /F` for tree cancellation and `tasklist.exe` for PID liveness checks.
- Added workflow APIs, SSE/event endpoints, and dashboard workflow controls.
- Added dedicated HTTP `409 Conflict` responses with `workflow_state_conflict` codes for workflow requests that are validly shaped but invalid in the current run/definition state.
- Hardened dashboard owner-session minting so static GET only mints a cookie for loopback client plus loopback Host.
- Hardened kill switch activation by latching the active flag first, revoking all owner sessions, then cancelling background work.
- Hardened learning shutdown during kill-switch activation so in-flight learning diagnostics finish before the `kill_switch_activate` audit row is written.
- Stopped scheduled routines from manufacturing owner approval.
- Stopped trusted routine agent steps from becoming owner-approved agent runs.
- Scoped trusted routine tool execution to declared routine tools and policy tier handling.
- Added immutable routine definition versions, exact routine run snapshots, owner-confirmed routine rollback, API endpoints, and dashboard version/rollback controls.
- Added deterministic disjoint train/validation/holdout export and prompt leakage detection.
- Added LoRA job discovery, artifact validation, evaluation persistence, promotion gates, owner-confirmed promotion, and rollback.
- Hardened LoRA promotion so API-submitted metrics are visible but not promotable; trusted evaluations bind the artifact digest and promotion fails if adapter files change afterward.
- Made workflow transition state changes atomic with their event/checkpoint rows for run start, retry reuse, node start, approval decisions, terminal node transitions, terminal run transitions, and cancellation requests.
- Added SQLite-trigger rollback tests proving transition state does not half-commit if event outbox insertion fails.

## Provider And Model Coverage

Project Q supports:

- Local heuristic mode with no API key.
- Local LLM mode through Ollama-compatible `/api/chat`.
- OpenAI Responses-compatible remote provider.
- Anthropic Messages-compatible remote provider.

The main suite includes provider-routing tests for Ollama, OpenAI Responses, Anthropic Messages, native streaming parsers, fallback behavior, and deterministic tool routing before stale provider output. Because the post-atomicity main suite timed out, rerun or isolate it before making a fresh all-green claim.

LoRA promotion currently records and guards an active adapter manifest. It does not automatically create or switch an Ollama serving model. This remains future PRD work, and the current real adapter did not pass the clean promotion gate.

## Security And Reliability Fixes From Audit

- Dashboard GET no longer grants owner sessions for private-network Host headers when the app is bound broadly.
- Kill switch revokes active owner sessions and blocks stale dashboard cookies.
- Kill switch now waits for an in-flight learning cycle to quiesce before writing the activation audit event, preventing later background diagnostics audit rows from obscuring the emergency event.
- Cancellation remains available during the kill switch after the owner obtains a new local session.
- Workflow terminal transitions use conditional updates so a late success cannot overwrite a cancelling run.
- Workflow invalid-state API responses use `409 Conflict` instead of generic bad-request paths.
- Node terminal and PID assignment updates check row counts to detect concurrent races.
- Workflow retry no longer reuses human approval decisions.
- Worker process launch cleanup cancels and closes post-launch failures.
- Windows worker cancellation targets the process tree.
- API-submitted LoRA evaluations cannot promote adapters.
- LoRA promotion checks the trusted-evaluator flag and adapter artifact digest before activation.
- Workflow state transitions now commit or roll back with their event/checkpoint outbox rows.

## Remaining Gaps

These are not hidden; they remain real PRD work:

- Full main suite timeout after latest atomicity work needs systematic debugging.
- Agent hierarchy metadata exists, but full supervisor orchestration, parent-run propagation from workflow workers, and root budget accounting are not complete.
- Reflection/playbook promotion and reusable post-run playbooks are still missing.
- Active LoRA adapter metadata is guarded, but serving integration for a promoted merged/Ollama model still needs a complete activation pipeline.
- The Codex in-app Browser plugin could not be used for UI verification in this Windows sandbox because the local bridge failed with Windows permission errors. Project Q's own browser engine was verified independently and passed.
- `verify_phase2_relay.py` requires relay dependencies; this runtime is missing `fastapi`.

## Decision

Phase 3 is substantially implemented for workflows, agents, browser worker behavior, provider routing, guarded LoRA lifecycle, and atomic workflow transition durability. The whole PRD is not complete because the remaining gaps above still require implementation and verification, and the latest full main suite run must be debugged.
