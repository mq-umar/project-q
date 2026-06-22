# Phase 3 Agent Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for each task and superpowers:verification-before-completion before claiming success.

**Goal:** Deliver durable, cancellable multi-agent workflows with versioned agent definitions, observable DAG execution, and leakage-safe LoRA evaluation and promotion.

**Architecture:** Keep the current single-agent and routine contracts working while adding immutable definition versions and a dedicated workflow domain. A coordinator persists every transition and dispatches nodes through a pluggable execution backend; production uses one subprocess per active node so cancellation and the kill switch can terminate work, while tests use deterministic in-process workers. SQLite is configured for bounded concurrent access, workflow events are append-only, and restart reconciliation resumes only persisted safe states.

**Tech Stack:** Python 3.12 standard library, SQLite, Pydantic models, existing Project Q HTTP server, vanilla JavaScript/CSS, unittest.

---

## Task 1: Harden SQLite for concurrent orchestration

**Files:**
- Modify: `src/project_q/storage.py`
- Test: `tests/test_phase3_storage.py`

1. Add failing tests for WAL mode, busy timeout, foreign-key enforcement, idempotent migrations, and concurrent writers.
2. Centralize connection setup with `timeout=30`, `PRAGMA busy_timeout`, `PRAGMA journal_mode=WAL`, and `PRAGMA foreign_keys=ON`.
3. Add migration helpers that are transaction-safe and rerunnable.
4. Run `python -m unittest tests.test_phase3_storage`.

## Task 2: Add immutable agent definitions and run snapshots

**Files:**
- Modify: `src/project_q/models.py`
- Modify: `src/project_q/storage.py`
- Modify: `src/project_q/services/agents.py`
- Modify: `src/project_q/services/agent_runner.py`
- Test: `tests/test_phase3_agent_definitions.py`

1. Add failing tests for immutable versions, built-in templates, custom v1 backfill, parent/root/depth hierarchy, budgets, success contracts, and exact run lookup.
2. Add `agent_definitions` and `agent_definition_versions`; pin each agent instance to a version.
3. Extend `agent_runs` with definition snapshot, parent/workflow linkage, budget/contract snapshot, usage, evaluation, and timestamps.
4. Replace insert-then-list run retrieval with exact inserted-row retrieval.
5. Stop using global agent status as the source of truth for concurrent runs; derive active state from run records.
6. Preserve existing agent CRUD, template, and run API response compatibility.
7. Run `python -m unittest tests.test_phase3_agent_definitions tests.test_project_q`.

## Task 3: Implement workflow definitions and DAG validation

**Files:**
- Create: `src/project_q/services/workflows.py`
- Modify: `src/project_q/models.py`
- Modify: `src/project_q/storage.py`
- Test: `tests/test_phase3_workflows.py`

1. Add failing tests for duplicate keys, missing dependencies, cycles, limits, topological ordering, fan-out/fan-in, dependency policies, and immutable versions.
2. Add workflow definition/version, run, node-run, event, and checkpoint tables.
3. Implement definition CRUD with 1-50 nodes and parallelism 1-8.
4. Support `all_success` and `all_done` dependencies plus fail, skip, and continue policies.
5. Store canonical definition snapshots on each workflow run.
6. Run `python -m unittest tests.test_phase3_workflows`.

## Task 4: Add durable coordinator and isolated node workers

**Files:**
- Create: `src/project_q/services/workflow_orchestrator.py`
- Create: `src/project_q/workflow_worker.py`
- Modify: `src/project_q/app.py`
- Modify: `src/project_q/config.py`
- Modify: `src/project_q/services/agent_runner.py`
- Test: `tests/test_phase3_orchestration.py`

1. Add failing tests for bounded parallel fan-out, fan-in, same-agent serialization, cancellation, timeout, failure propagation, retry, and restart reconciliation.
2. Define a node backend protocol and deterministic test backend.
3. Implement the production subprocess backend using the current Python executable and persisted run/node identifiers.
4. Add cooperative cancellation checks around reasoner/tool boundaries and hard process termination after a bounded grace period.
5. Make the global kill switch stop scheduling and cancel active workflow subprocesses; cancellation remains available while the switch is active.
6. Persist transitions and checkpoints before and after every dispatch.
7. Add worker mode so child processes do not start schedulers, relays, or another orchestrator.
8. Reconcile stale queued/running/cancelling work at startup.
9. Run `python -m unittest tests.test_phase3_orchestration tests.test_project_q`.

## Task 5: Expose workflow APIs and event streaming

**Files:**
- Modify: `src/project_q/server.py`
- Modify: `src/project_q/models.py`
- Test: `tests/test_phase3_workflow_api.py`

1. Add failing owner-session tests for workflow CRUD, start, list, detail, result, cancel, retry, event cursoring, and kill-switch behavior.
2. Add `/api/workflows`, `/api/workflow-runs`, and nested run endpoints.
3. Return `202` for accepted asynchronous runs and `409` for invalid state transitions.
4. Add ordered event polling and SSE streaming with cookie authentication and redacted payloads.
5. Keep cancellation exempt from the kill-switch start gate.
6. Run `python -m unittest tests.test_phase3_workflow_api tests.test_project_q`.

## Task 6: Build the workflow dashboard

**Files:**
- Modify: `src/project_q/static/index.html`
- Modify: `src/project_q/static/app.js`
- Modify: `src/project_q/static/styles.css`
- Test: `tests/test_phase3_workflow_ui.py`

1. Add failing static/UI contract tests for controls, labels, status text, progress, and event rendering.
2. Add Workflows navigation between Agents and Routines.
3. Add definition editor, DAG/topological preview, run controls, progress, cancel/retry, event log, and results.
4. Use EventSource with polling fallback and preserve keyboard/screen-reader behavior.
5. Replace existing mojibake characters with valid UTF-8 or ASCII equivalents.
6. Verify desktop and mobile layout in the in-app browser.

## Task 7: Repair LoRA evaluation and add guarded promotion

**Files:**
- Modify: `src/project_q/services/training.py`
- Modify: `src/project_q/models.py`
- Modify: `src/project_q/storage.py`
- Modify: `src/project_q/server.py`
- Modify: `src/project_q/static/index.html`
- Modify: `src/project_q/static/app.js`
- Test: `tests/test_phase3_training_lifecycle.py`

1. Add failing tests for training job discovery, status, artifact validation, exact/normalized holdout leakage, baseline comparison, metrics persistence, and promotion gates.
2. Generate deterministic disjoint train/validation/holdout datasets and reject leaked evals.
3. Evaluate both base and adapter with the same prompts and record outputs, accuracy, latency, memory, and failures.
4. Add job states: prepared, training, trained, evaluating, passed, failed, promoted, and archived.
5. Require artifact validation, clean holdouts, configurable minimum score, and no baseline regression before promotion.
6. Never auto-promote; expose an owner-confirmed promote action and a rollback target.
7. Import the completed `lora_job_e5db0a2a9c95` as trained but untrusted until it passes a clean comparison.
8. Run `python -m unittest tests.test_phase3_training_lifecycle tests.test_project_q`.

## Task 8: End-to-end verification and audit

**Files:**
- Create: `docs/PHASE_3_AUDIT_2026-06-14.md`
- Modify: `HANDOFF.md`

1. Run Python compile checks and JavaScript syntax checks.
2. Run the complete application, relay, and Phase 1/2/3 verification suites.
3. Simulate successful, failed, timed-out, cancelled, retried, and restart-recovered workflows.
4. Exercise browser automation through the dashboard and verify screenshots at desktop/mobile sizes.
5. Audit authentication, authorization, SSRF, path traversal, command construction, secret redaction, subprocess isolation, SQLite contention, event injection, and denial-of-service limits.
6. Run clean base-versus-adapter LoRA evaluation and document whether promotion is justified.
7. Record residual risks and exact PRD coverage in the audit and handoff.
