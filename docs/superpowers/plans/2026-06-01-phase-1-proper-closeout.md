# Phase 1 Proper Closeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the concrete Phase 1 gap left in the handoff by turning the Windows automation inventory from 14 verified tools into 17 registered, tested tools.

**Architecture:** Keep all new Windows automation in `src/project_q/tools/windows.py`, register it in `ToolRegistry`, expose default payloads in the dashboard, and make `verify_phase1.py` assert the final inventory. Use bounded, encoded PowerShell and artifact path confinement for anything touching screenshots.

**Tech Stack:** Python 3.12, unittest, SQLite app fixture, PowerShell via encoded subprocess calls, static HTML/CSS/JS dashboard.

---

### Task 1: Add Failing Regression Tests

**Files:**
- Modify: `tests/test_project_q.py`

- [x] **Step 1: Write failing tests for the three missing Windows tools**

Add tests that import and exercise:
- `WindowsAppStateTool`
- `WindowsFocusFollowTool`
- `WindowsScreenshotDiffTool`

Also assert that the app registry contains 17 `windows.*` tools.

- [x] **Step 2: Run the targeted tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
& 'C:/Users/umarq.APEX/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m unittest tests.test_project_q.ProjectQApplicationTests.test_windows_phase1_tool_inventory_is_seventeen tests.test_project_q.ProjectQApplicationTests.test_windows_app_state_requires_selector_and_reports_processes tests.test_project_q.ProjectQApplicationTests.test_windows_focus_follow_uses_encoded_sta_powershell tests.test_project_q.ProjectQApplicationTests.test_windows_screenshot_diff_confines_artifacts_and_compares_images
```

Expected: fail because the new classes/tools do not exist yet.

### Task 2: Implement Missing Windows Tools

**Files:**
- Modify: `src/project_q/tools/windows.py`
- Modify: `src/project_q/tools/registry.py`

- [x] **Step 1: Add `WindowsAppStateTool`**

Tool id: `windows.app_state`. Tier 0. Query process/window state by `process_id`, `process_name`, or `window_title`; return bounded process/window metadata without mutating desktop state.

- [x] **Step 2: Add `WindowsFocusFollowTool`**

Tool id: `windows.focus_follow`. Tier 1. Find a visible window by `query`, `process_name`, or ranked `candidates`, then activate the best match through `WScript.Shell.AppActivate`.

- [x] **Step 3: Add `WindowsScreenshotDiffTool`**

Tool id: `windows.screenshot_diff`. Tier 0. Resolve both screenshot inputs through the existing `.project_q/windows_artifacts` confinement helper, then use PowerShell/System.Drawing to compare dimensions and sampled pixels.

- [x] **Step 4: Register the three tools**

Import and instantiate the tools in `src/project_q/tools/registry.py`.

- [x] **Step 5: Run targeted tests and verify GREEN**

Run the Task 1 command again. Expected: pass.

### Task 3: Expose And Verify The Final Inventory

**Files:**
- Modify: `verify_phase1.py`
- Modify: `src/project_q/static/app.js`
- Modify: `src/project_q/services/diagnostics.py`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `HANDOFF.md`

- [x] **Step 1: Update `verify_phase1.py`**

Add the three new tool ids to `EXPECTED_WINDOWS_TOOLS` and update the checklist text to "17 registered windows tools."

- [x] **Step 2: Update dashboard payloads**

Add defaults for:
- `windows.app_state`
- `windows.focus_follow`
- `windows.screenshot_diff`

- [x] **Step 3: Update diagnostics/documentation**

Include the three tools in diagnostics and remove the handoff caveat that treated them as future work.

### Task 4: Final Verification

**Files:**
- No code edits expected.

- [x] **Step 1: Run full Python unit tests**

Expected: all tests pass.

- [x] **Step 2: Run compile/static checks**

Run `compileall`, `node --check`, and `git diff --check`.

- [x] **Step 3: Run isolated Phase 1 verifier**

Expected: 17 Windows tools, 64 total tools unless another registry group changes.

- [x] **Step 4: Live smoke running server**

Persistent restart on `http://127.0.0.1:8787` was blocked by the approval/usage gate, so the live smoke used an ephemeral localhost server from the new build and verified `/api/status`, `/api/tools`, `/`, `/app.js`, and malformed JSON handling.
