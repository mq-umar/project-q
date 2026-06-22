# Project Q - Claude Handoff

Prepared: 2026-06-21
Primary worktree: `C:\Users\umarq.APEX\Documents\Jarvis\.worktrees\phase3-orchestration`
Branch: `codex/phase3-orchestration`

## One-Line State

Project Q Phase 1 and Phase 2 foundations are implemented and previously verified; Phase 3 orchestration, workflow runtime, browser worker hardening, provider routing, guarded LoRA lifecycle, and atomic workflow transition outbox work are implemented. The project is not PRD-complete yet. The current outstanding verification blocker is the full `tests.test_project_q` suite timing out after the latest atomicity patch, even though the focused workflow and full Phase 3 suites pass.

## 2026-06-21 Security & Reliability Audit

A multi-agent audit (45 raw → 43 confirmed findings, each adversarially verified)
was completed and ~30 fixed this pass — see `docs/AUDIT_RESPONSE_2026-06-21.md` for
the per-finding disposition. The full unittest suite (333 tests, including the new
`tests/test_audit_fixes.py`) passes. Note: the main suite is timing-sensitive for
live-HTTP streaming tests under heavy load; re-run any flaky socket timeout in
isolation before treating it as a regression.

## User Goal

The user wants the entire PRD completed and actually functioning, including:

- Local LLM use without mandatory API keys.
- Optional API-key providers such as OpenAI and Anthropic.
- Browser control that really works, not only mocked behavior.
- Simulations and audits for bugs, security issues, and PRD gaps.
- Phase 1 completed properly and continued progress through the rest of the PRD.

Do not mark the overall goal complete yet. There are real remaining PRD gaps listed below.

## Current Verification Truth

Post-atomicity work that passed:

- `python -m compileall -q src/project_q/services/workflow_service.py`: passed.
- Workflow atomic rollback regression subset: 7 tests passed in 4.253 seconds.
- Focused workflow suite:
  - `tests.test_phase3_workflow_service`
  - `tests.test_phase3_workflow_api`
  - `tests.test_phase3_workflow_orchestrator`
  - `tests.test_phase3_workflow_runtime`
  - `tests.test_phase3_workflow_process`
  - `tests.test_phase3_workflow_e2e`
  - `tests.test_phase3_workflow_graph`
  - `tests.test_phase3_workflow_ui`
  - Result: 67 tests passed in 47.876 seconds.
- Full Phase 3 suite: 117 tests passed in 112.007 seconds.

Important current blocker:

- Full main suite command:
  - `$env:PYTHONPATH='src'; & 'C:/Users/umarq.APEX/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m unittest tests.test_project_q`
  - Result after latest atomic workflow patch: timed out after about 604 seconds with no useful output.
  - Previous pre-atomicity run passed 201 tests in 398.874 seconds, but do not claim the current main suite is green until it is rerun or isolated.
  - A stale bundled Python process from the timed-out verification was stopped.

Previously verified before the latest atomicity patch:

- `python -m compileall -q src`: passed.
- `node --check src/project_q/static/app.js`: passed.
- `verify_phase1.py`: passed, including Project Q's real browser worker self-test.
- `verify_phase2_backend.py`: passed.
- `verify_phase2_relay.py`: blocked because this runtime is missing `fastapi`.
- Live HTTP workflow simulation: completion path reached `completed`; cancellation path reached `cancelled`.
- Browser worker self-test passed launch, form fill, tabs, upload, download, screenshot, persistent profile state, private HTTP blocking, and private WebSocket blocking.

## What Changed Most Recently

The latest work focused on the PRD/security gap where workflow state transitions and their event/checkpoint outbox rows were not committed atomically.

Files involved:

- `src/project_q/services/workflow_service.py`
- `tests/test_phase3_workflow_service.py`
- `HANDOFF.md`
- `docs/PHASE_3_AUDIT_2026-06-16.md`

Implemented behavior:

- Public `append_event` and `append_checkpoint` remain available for explicit append-only use.
- Transition paths now use private helpers that insert events/checkpoints inside the same SQLite transaction as the state update:
  - `_insert_event(...)`
  - `_insert_checkpoint(...)`
  - `_node_run_from_conn(...)`
- Atomic transition coverage now includes:
  - `start_run`
  - retry with reused successful nodes
  - `mark_node_running`
  - owner approval decisions
  - node terminal transitions
  - run terminal transitions
  - cancellation requests
- Added rollback tests that create SQLite triggers to abort specific `workflow_events` inserts and prove the related state update does not half-commit.

New important tests in `tests/test_phase3_workflow_service.py`:

- `test_node_start_rolls_back_when_event_outbox_insert_fails`
- `test_start_run_rolls_back_when_creation_outbox_insert_fails`
- `test_retry_rolls_back_when_reuse_outbox_insert_fails`
- `test_approval_decision_rolls_back_when_outbox_insert_fails`
- `test_node_terminal_rolls_back_when_outbox_insert_fails`
- `test_run_terminal_rolls_back_when_outbox_insert_fails`
- `test_cancel_rolls_back_when_outbox_insert_fails`

## Implemented PRD Coverage

Phase 1:

- Voice and text conversational loop.
- Local browser speech recognition/TTS plus Windows SAPI fallback tools.
- Memory system with SQLite persistence, FTS5 search, provenance, retention, and pruning.
- Web research with citations and bounded browsing behavior.
- Filesystem tools with path controls.
- Real browser automation through Project Q's worker, including visible persistent browser profiles.
- Sandboxed terminal execution with explicit trusted host mode.
- Email/calendar draft and Outlook COM integrations behind settings.
- Task planning and CRUD.
- Append-only audit log.
- Windows DPAPI-backed secrets vault.
- Basic research/coding agents.
- Agent templates, metrics, scheduler, and memory prune APIs.

Phase 2:

- Provider routing supports:
  - local heuristic mode with no API key,
  - Ollama-compatible local LLM chat,
  - OpenAI Responses-compatible remote provider,
  - Anthropic Messages-compatible remote provider.
- Native streaming parsers and fallback behavior are tested.
- Structured execution waits for complete validated plans before mutating memory, tasks, agents, routines, or tools.
- Browser worker runtime discovery includes bundled dependency trees so Playwright can launch.

Phase 3:

- SQLite WAL, busy timeout, foreign keys, rerunnable migrations, and concurrent writer coverage.
- Immutable agent definition versions and exact run snapshots.
- Agent hierarchy metadata, parent/root/depth fields, budgets, success contracts, tool whitelists, required terms/tools/artifacts, minimum score, and time budget.
- Draft and blocked agents cannot start runs.
- Durable workflow definitions, immutable versions, run/node snapshots, ordered events, checkpoints, retries, cancellation, timeouts, restart reconciliation, and dashboard/API controls.
- Workflow APIs and event streaming.
- Dedicated HTTP `409 Conflict` responses with `workflow_state_conflict` for invalid workflow state transitions.
- Human approval workflow nodes pause for owner decisions and are not replayed during retry.
- Subprocess workflow workers with bounded logs, custom data-root/db-path propagation, worker mode, timeout handling, Windows process-tree cancellation, and cleanup on launch/PID persistence failures.
- Kill switch latches first, revokes active sessions, cancels work, and waits for in-flight learning diagnostics to quiesce before the activation audit row.
- Scheduled routines no longer synthesize owner approval.
- Trusted routine agent steps no longer become owner-approved runs.
- Routine execution is scoped to declared tools and policy tiers.
- Immutable routine definition versions, run snapshots, owner-confirmed rollback, API endpoints, and dashboard controls.
- LoRA lifecycle with disjoint train/validation/holdout export, prompt leakage detection, base-vs-adapter evaluation persistence, promotion gates, owner-confirmed promotion, rollback, trusted evaluator binding, and adapter artifact digest checking.
- Atomic workflow transition event/checkpoint outbox work is now implemented and covered by rollback tests.

## Browser Control Notes

The user's prior pain point was that Project Q could not really control the browser. Current state:

- Project Q's own browser worker has been directly verified.
- Verified capabilities include:
  - launch,
  - form interaction,
  - tab control,
  - upload,
  - download,
  - screenshot,
  - persistent profile/cookie state,
  - private HTTP blocking,
  - private WebSocket blocking.
- Service workers are disabled in controlled contexts.
- The Codex in-app Browser plugin bridge had Windows permission problems earlier, so do not confuse that with Project Q's browser engine. Project Q's browser worker itself passed its self-test.

## Local And API LLM Notes

Provider support exists in the app:

- Local heuristic: works without API keys.
- Ollama-compatible local LLM: `/api/chat` style local provider path.
- OpenAI Responses-compatible provider.
- Anthropic Messages-compatible provider.

The main suite includes provider-routing tests for Ollama, OpenAI, Anthropic, native streaming parsers, fallback behavior, and deterministic tool routing before stale provider output. Because the current main suite timed out after the latest atomicity patch, rerun or isolate before making a fresh green claim.

## Known Remaining PRD Gaps

Do not hide these:

- Full main suite currently needs investigation after the latest atomicity patch timed out at about 604 seconds.
- Full supervisor orchestration and root budget propagation are not complete. Hierarchy metadata exists, but root budget accounting and parent-run propagation from workflow workers still need deeper implementation.
- Reflection/playbook promotion and reusable post-run playbooks are still missing.
- LoRA promotion records a guarded active adapter manifest, but it does not yet create/switch an Ollama serving model automatically.
- First real LoRA adapter `lora_job_e5db0a2a9c95` is valid but inactive. It scored 10/12 on clean holdout, below the 0.85 promotion gate.
- Relay verifier is blocked in this runtime because `fastapi` is not installed.
- Codex in-app Browser plugin verification was blocked by Windows permission errors, though Project Q's browser worker passed independently.

## Immediate Next Steps For Claude

1. Investigate the full `tests.test_project_q` timeout before claiming all tests pass.
2. Use systematic debugging, not guesswork:
   - rerun with `-v` to capture the last printed test,
   - split `tests/test_project_q.py` by test names or classes,
   - compare any hang to workflow transition changes,
   - look for nested reads while write transactions are still open.
3. Suspect areas worth inspecting if the hang is workflow-related:
   - `WorkflowService.set_run_terminal`
   - `WorkflowService.request_cancel`
   - `WorkflowService.mark_node_terminal`
   - branches that call `get_run(...)` or `get_node_run(...)` while still inside a write transaction.
4. After fixing or isolating the timeout, rerun:
   - `python -m compileall -q src`
   - `node --check src/project_q/static/app.js`
   - full Phase 3 suite
   - `tests.test_project_q`
   - `verify_phase1.py`
   - `verify_phase2_backend.py`
   - relay verifier only after installing relay dependencies or using an environment with `fastapi`
5. Update this handoff and `docs/PHASE_3_AUDIT_2026-06-16.md` with the new verification truth.

## Useful Commands

Set up paths:

```powershell
Set-Location "C:\Users\umarq.APEX\Documents\Jarvis\.worktrees\phase3-orchestration"
$env:PYTHONPATH = "src"
$py = "C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$node = "C:\Users\umarq.APEX\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
```

Run app:

```powershell
& $py -m project_q.server
```

Server URL:

```text
http://127.0.0.1:8787
```

Focused workflow suite:

```powershell
& $py -m unittest tests.test_phase3_workflow_service tests.test_phase3_workflow_api tests.test_phase3_workflow_orchestrator tests.test_phase3_workflow_runtime tests.test_phase3_workflow_process tests.test_phase3_workflow_e2e tests.test_phase3_workflow_graph tests.test_phase3_workflow_ui
```

Full Phase 3 suite:

```powershell
& $py -m unittest tests.test_phase3_agent_definitions tests.test_phase3_control tests.test_phase3_lora_evaluator tests.test_phase3_routine_runner tests.test_phase3_routine_versions tests.test_phase3_storage tests.test_phase3_training_api tests.test_phase3_training_evaluation tests.test_phase3_training_lifecycle tests.test_phase3_training_ui tests.test_phase3_workflow_api tests.test_phase3_workflow_e2e tests.test_phase3_workflow_graph tests.test_phase3_workflow_orchestrator tests.test_phase3_workflow_process tests.test_phase3_workflow_runtime tests.test_phase3_workflow_service tests.test_phase3_workflow_ui
```

Main suite with verbose output for timeout isolation:

```powershell
& $py -m unittest -v tests.test_project_q
```

Syntax checks:

```powershell
& $py -m compileall -q src
& $node --check src/project_q/static/app.js
```

Phase verifiers:

```powershell
& $py -X utf8 verify_phase1.py
& $py -X utf8 verify_phase2_backend.py
& $py -X utf8 verify_phase2_relay.py
```

## Git/Worktree Notes

The worktree has many modified and untracked files from Phase 1/2/3 work. Treat them as intentional unless proven otherwise. Do not revert unrelated user or prior-agent changes.

Expected broad areas with changes:

- `src/project_q/app.py`
- `src/project_q/config.py`
- `src/project_q/models.py`
- `src/project_q/server.py`
- `src/project_q/storage.py`
- `src/project_q/services/*`
- `src/project_q/static/*`
- `src/project_q/workflow_worker.py`
- `src/project_q/lora_evaluator.py`
- `tests/test_project_q.py`
- `tests/test_phase3_*.py`
- `docs/*`
- `.browser_engine_qa/*`

`src/project_q/services/workflow_service.py` and many Phase 3 tests are untracked relative to the repository base, so `git diff` alone may not show their content. Use `rg`, `Get-Content`, or `git status --short` as needed.

## Audit Posture

The project should be treated as a security-sensitive local agent:

- Keep owner approval boundaries strict.
- Keep kill switch behavior conservative.
- Keep audit logs append-only.
- Keep browser automation bounded and private-network protections intact.
- Keep secrets names visible but values protected.
- Keep local LLM and remote API providers interchangeable without forcing API keys.
- Treat API-submitted LoRA metrics as untrusted.
- Do not promote adapters without trusted holdout evaluation and artifact digest checks.
- Do not allow workflow state changes without matching event/checkpoint durability.

## Final Instruction To Claude

Continue from the current worktree, not from scratch. The highest-value next move is to isolate and fix the full main suite timeout, then rerun verification and continue the remaining PRD gaps. Do not claim "complete" until all required PRD features and verifications are actually green.
