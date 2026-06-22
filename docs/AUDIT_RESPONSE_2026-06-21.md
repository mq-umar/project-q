# Project Q — Security & Reliability Audit Response (2026-06-21)

A multi-agent audit (53 agents, every finding adversarially verified against the
real code) surfaced **45 raw findings → 43 confirmed** (5 high, 19 medium, 19 low;
2 rejected by verification). This document records the disposition of every
confirmed finding: **Fixed**, **By design / mitigated**, or **Deferred (gated)**.

Validation: `python -m compileall` clean, `node --check app.js` clean, the full
unittest suite plus a new `tests/test_audit_fixes.py` (15 targeted regressions).

---

## Fixed (this pass)

### HIGH
| ID | Fix |
|----|-----|
| `model-set-trusted-routine-escalation` | `PlanExecutorService.execute` now forces `trusted=False` for every model-authored routine; trust is only granted by an explicit owner action. |
| `external-origin-tag-lost-on-replan` | External tool outputs (browser/outlook) are run through `TrustBoundaryService.scan_external_content`; `compose_reply` surfaces the labeled **UNTRUSTED EXTERNAL CONTENT** envelope, so persisted/re-injected content stays tagged as Zone 3 data. |
| `screen-context-auto-trusted-untagged` | `ContextService.build` scans on-screen OCR through the trust boundary and embeds only the safe-summary envelope. |
| `approval-policy-missing` | New `approval_policy` profile (`always_ask`/`ask_on_risky`/`trusted_routines_only`) added to settings, `SettingsUpdate`, the dashboard, and **enforced** in `PolicyService`. |
| `loopback-equals-owner-no-auth` | Sessions are now **reused** instead of minted on every page load; a 7-day GC purges dead sessions; and an **optional owner passphrase** (PBKDF2-SHA256, stdlib) gates session minting via `POST /api/auth/login` when enabled. Default off preserves the local one-click flow. |

### MEDIUM
| ID | Fix |
|----|-----|
| `browser-outlook-read-no-trust-scan` | Covered by the central external-output trust-scan in `PlanExecutorService`. |
| `auto-approve-tier-allows-tier3` | `auto_approve_tier` capped at 2 in `SettingsUpdate`; `PolicyService` additionally hard-blocks Tier 3 auto-approval (defense in depth). |
| `vault-silent-plaintext-fallback` | Secrets blobs carry a versioned `PQv1` scheme tag (`D`=DPAPI, `P`=plaintext-dev); `_decrypt` validates the tag and refuses cross-scheme/unknown blobs. Backward compatible with legacy blobs. |
| `unknown-tool-id-crashes-turn` | Registry lookup is guarded; a hallucinated/injected `tool_id` is recorded as a blocked tool instead of aborting the turn. |
| `workflow-sse-double-response-on-windows-disconnect` | The workflow SSE handler swallows `OSError` (incl. `ConnectionAbortedError`) so a disconnect cannot trigger a second HTTP response. |
| `orphan-agent-run-on-node-kill` | The orchestrator reconciles `agent_runs` left `running` after their owning workflow node terminates (`list_running_workflow_node_runs` + `fail_run_for_workflow_node`). |
| `aggression-level-not-enforced` | `aggression_level` now modulates the effective auto-approval bar in `PolicyService`. |
| `proactive-mode-not-enforced` | `proactive_mode="off"` suppresses proactive follow-up task creation in the learning loop. |
| `reflection-no-playbook-promotion` | `reflect_on_task` now emits all PRD §6.2 outputs: outcome assessment, assumption audit, model-performance note, memory update, and an owner-promotable (never auto-trusted) procedural **playbook candidate**. |
| `lora-no-serving-switch` | `promote_lora_job` now materializes an Ollama model (`ollama create`) and routes inference to it; degrades gracefully (records intent) when a GGUF artifact or `ollama` is absent. |
| `autorefresh-clobbers-active-chat-stream` | 30s auto-refresh is guarded by a `chatStreaming` flag (frontend). |
| `no-visible-focus-indicator` | Global `:focus-visible` ring added (frontend). |
| `placeholder-navlabel-contrast-fail` | `--muted`/`--muted-2` tokens lightened to clear WCAG contrast (frontend). |

### LOW
| ID | Fix |
|----|-----|
| `server-error-leaks-internal-exception-text` | Both catch-all 500 handlers return a generic message; detail stays in the audit log only. |
| `fts-full-rebuild-every-startup` | FTS index is rebuilt only when out of sync with `memories` (first creation over a legacy DB), not on every boot. |
| `prune-expired-cutoff-timezone-suffix-mismatch` | Cutoff is formatted with the same `Z` suffix as `utc_now()`. |
| `workflow-events-checkpoints-unbounded-growth` | `WorkflowService.prune_history` discards the event/checkpoint trail of terminal runs older than 30 days; run at orchestrator start. |
| `voice-mode-profile-missing` | `voice_mode` profile added to settings, `SettingsUpdate`, and the dashboard. |
| `owner-session-unbounded-mint-no-gc` | Session reuse + 7-day GC (see HIGH auth fix). |
| `unbounded-negative-limit-query-params` | `conversation.list_messages` clamps `limit` to `[1, 500]`. *(Other list endpoints: see Deferred.)* |
| `markdown-no-link-rendering` | Safe autolink (http/https/mailto only, escaped, `rel=noopener`) added to the markdown renderer (frontend). |
| `empty-conversation-no-state` / `no-reduced-motion-support` / `tool-payload-not-reset-on-missing-example` / `status-pill-error-overwritten-on-retry` | All addressed (frontend). |

---

## By design / mitigated (no code change, documented)

| ID | Rationale |
|----|-----------|
| `companion-chat-hardcodes-owner-source` | Owner-initiated chat (dashboard **and** companion) legitimately carries owner authority — stripping it would break the product. Injection is instead defended by **labeling external content as untrusted data** (the trust-envelope fix above), which is the PRD §9 intent ("Zone 3 can inform reasoning, never directly authorize"). |
| `promotion-impossible-trusted-evaluator-always-false` | The HTTP eval route passes `trusted_evaluator=False` **intentionally** (API-submitted metrics are untrusted). `record_lora_evaluation` defaults `trusted_evaluator=True` for owner-side/CLI evaluation, which is the supported promotion path. |
| `kill-switch-no-auth-csrf-empty-origin` | The existing Origin allow-list already rejects cross-origin browser requests (the real CSRF vector); the empty-Origin allowance is required for non-browser API/companion clients. Kill-switch is intentionally reachable for emergency use on the loopback-only bind. |
| `dpapi-user-scope-no-entropy` | Static machine-local entropy adds obscurity, not strength; secrets remain DPAPI user-scoped. Low value. |

---

## Deferred (dependency- or scope-gated — tracked, not done)

| ID | Why deferred / what it needs |
|----|------------------------------|
| `data-at-rest-not-encrypted` | Full-DB AES-at-rest needs **SQLCipher** (`pysqlcipher3`), not available in the bundled runtime. Zone-0 secrets are already DPAPI-encrypted. Recommended upgrade: open SQLite via SQLCipher with a DPAPI-sealed key. |
| `no-vector-embedding-memory-index` | True semantic memory needs an embedding model + vector index. Recommended dependency-free path: call Ollama's `/api/embeddings` when configured, hybridized with the existing FTS5 lexical index. |
| `procedural-source-memory-layers-missing` | **Partially addressed** — reflection now writes `procedural` memories (playbook candidates). Full source-memory (origin/trust_level/inferred columns) is a schema migration deferred to avoid destabilizing the memory test surface. |
| `lora-holdout-not-bound-to-frozen-evalset` | Binding the caller holdout to the job's frozen eval set risks the extensive training test suite; deferred as a hardening with a clear implementation note. |
| `cancel-blocks-orchestrator-under-iteration-lock` | Making cancellation fully non-blocking is a concurrency redesign; acceptable for single-owner local use. |
| `workflow-service-no-immediate-lock-readmodifywrite` | Writes are already atomic within a single connection; the cross-connection read-then-write race is low-severity for a single operator. |
| `agent-supervisor-budget-not-propagated` | Root-budget propagation across child runs is a PRD §7 feature; deferred. |
| `legacy-companion-complete-pairing-route-live` | Removing the legacy v1 pairing/quick-capture routes is good hygiene but needs full call-path verification; deferred to avoid regressions. |
| `nl-policy-editor-missing` | PRD §16.1 future-phase enhancement. |
| remaining `unbounded-negative-limit` paths | `agent_runner.list_runs`, `routine_runner.list_runs`, learning lists — same clamp pattern; low severity (owner-authed). |

---

## Environment constraints (cannot be exercised in this runtime)

The bundled runtime has only `cryptography` + `pydantic`. The FastAPI relay, live
Playwright browser worker, and the native iOS/Xcode app are **external release
gates** and are not runnable here — consistent with prior handoffs.
