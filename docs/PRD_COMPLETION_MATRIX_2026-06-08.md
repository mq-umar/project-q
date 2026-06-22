# Project Q PRD Completion Matrix

Source: `C:\Users\umarq.APEX\Downloads\Project_Q_PRD.docx`
Audit date: 2026-06-08

This matrix treats a roadmap item as complete only when current code, tests, and
runtime evidence cover the behavior described in the PRD. Registration,
placeholder UI, schema-only support, and adjacent features count as partial.

## Phase 1 - Windows MVP

| Roadmap requirement | Status | Current evidence |
|---|---|---|
| Voice and text loop with streaming STT/TTS | Complete | Browser continuous/interim STT, streamed speech synthesis, Windows fallback, conversation SSE tests |
| Episodic and semantic memory | Complete | SQLite persistence, FTS5 search, context assembly, retention and edit controls |
| Multi-source web research | Complete | `research.web`, source diversity, provenance, partial errors, Zone 3 scanning |
| File system and Playwright automation | Complete | Bounded file tools; real Playwright launch/form/tab/download/screenshot/persistence verification; visible isolated action profiles; public HTTP/WebSocket guards |
| Sandboxed terminal | Complete with prerequisite | Docker sandbox contract is tested and fails closed; Docker Desktop must be running |
| Email and calendar read and draft | Complete with environment prerequisite | RFC822/ICS drafts and opt-in Outlook COM integration |
| Task planning and tracking | Complete | Task CRUD, priorities, status, due dates, planner integration |
| Audit log | Complete | Immutable SQLite triggers, provenance, approval, model, outcome, errors |
| Secrets vault | Complete | Windows DPAPI storage and plaintext-leak tests |
| Research and Coding agent spawning | Complete | Templates, bounded execution, run persistence, audit records |

Current regression evidence: 199 passing application tests and the isolated
`verify_phase1.py` behavioral verifier.

## Phase 2 - iPhone Companion

| Roadmap requirement | Status | Current evidence / external gate |
|---|---|---|
| Voice/text chat and session sync | Complete in source and simulation | SwiftUI push-to-talk/text chat, provider-token direct SSE, encrypted-relay fallback, synchronized history, speech output, encrypted cache, and offline queue. Physical-device voice/network validation remains an Apple release gate. |
| Push notifications and approval flows | Complete in source and simulation | APNs registration, encrypted route storage, generic no-content wakes, approval categories/deep links, durable approval requests, biometric P-256 signatures, at-most-once execution, and completed history. Live APNs delivery requires owner Apple credentials and a device. |
| Remote Windows routine trigger | Complete | Companion-authenticated direct and relay endpoints plus native routine list/run UI and recent outcomes. |
| App Shortcuts and Siri | Complete in source | App Intents/App Shortcuts for ask, capture, routine run, and emergency stop. Siri/Action Button validation requires signed device installation. |
| End-to-end encrypted relay | Complete | X25519 + HKDF-SHA256 + AES-256-GCM, direction-separated keys, replay protection, durable idempotency, ciphertext-only FastAPI relay, WebSocket/poll delivery, presence, revocation, Docker self-hosting, and redirect-safe credential handling. |
| Live task status dashboard | Complete with bounded refresh | Task/agent state, running indicators, recent run outcomes, offline queue, approval state, emergency control, widget summary, 15-second sync polling, and push wakes. |
| Quick capture and memory browser | Complete | Text/voice/URL capture, share extension, encrypted offline queue, memory search/edit/delete, and biometric-gated memory access. |

Additional Phase 2 PRD surfaces implemented: redacted biometric-gated audit
viewer, widget, share extension, BackgroundTasks, presence, device revocation,
and remote emergency stop. `project.yml` is a complete XcodeGen definition, but
the generated `.xcodeproj`, signing, native tests, simulator, and real-device
checks require macOS/Xcode and remain release gates.

## Phase 3 - Full Agent System

| Roadmap requirement | Status | Gap to completion |
|---|---|---|
| Full agent factory with built-in types | Partial | Nine templates and custom records exist; no versioned definitions, explicit success contracts, resource budgets, parent/child hierarchy, or complete supervisor |
| Agent chaining and parallel coordination | Missing | Runs are synchronous and independent; no dependency graph, fan-out/fan-in, cancellation, or shared progress events |
| Website/full-stack creation pipeline | Partial | Prompt-aware generators and dispatch records exist; no general iterative build-test-fix-preview-deploy pipeline |
| Trusted routines with snapshot/rollback | Partial | Trusted routines and per-file overwrite snapshots exist; routine data scope, tier ceiling, rollback plan, and restore execution are absent |
| Procedural memory and reusable playbooks | Partial | `procedural` is accepted as a memory kind, but no playbook versioning, promotion, execution, or outcome linkage exists |
| Reflection and outcome learning | Partial | Task reflection and Learning Lab records exist; full post-agent/session assumption audit, owner review, playbook promotion, and model-performance learning are absent |
| Productivity connectors | Mostly missing | Outlook and local git exist; Slack/Teams, Notion/notes, cloud drives, task managers, Gmail, GitHub API, and deployment connectors are not implemented |
| Wake-word activation | Missing | Dashboard voice requires an open browser and explicit microphone action |
| Screen context with per-app consent | Partial | Screenshot/OCR auto-context exists; no per-app consent registry, live state model, expiration, or owner-visible capture history |

## Phase 4 - Intelligence and Optimization

| Roadmap requirement | Status | Gap to completion |
|---|---|---|
| Local model for intent/offline commands | Complete | Ollama provider, model discovery, task routing, native streaming, heuristic fallback |
| Preference learning and proactive suggestions | Partial | Settings, reflection memories, and preference training exports exist; no behavioral preference inference or proactive trigger engine |
| Multi-agent parallel work with live dashboard | Missing | No concurrent orchestration service or event/progress dashboard |
| Plugin/skill system for custom tools | Missing | Tool registration is code-defined at startup; no owner-installable manifest, permissions, lifecycle, or isolation model |
| Performance profiling and routing optimization | Partial | Basic success/failure metrics and model routing exist; no latency/cost/quality benchmark store or adaptive router |
| Self-hostable relay | Complete | Deployable FastAPI/SQLite relay, Docker hardening, opaque envelope routing, scoped tokens, presence, APNs bridge, encrypted APNs routes, and revocation are implemented and simulated. |

## Cross-Cutting PRD Gaps

These requirements are broader than a single roadmap bullet and remain open:

- Local application data is not encrypted at rest with AES-256. DPAPI protects
  Vault secrets, but conversations, tasks, memory, and audit data remain normal
  SQLite records.
- The Windows runtime is a local web dashboard, not an MSIX-packaged WinUI/Tauri
  shell with tray icon, startup registration, global hotkeys, or wake word.
- Kill switch state blocks new actions, but the PRD also requires terminating
  active agent workers and revoking all active sessions.
- Raw screenshot/audio expiration, one-click complete data export/delete, and
  audit replay with the full decision context are incomplete.
- The connector catalog and per-connector scope/revocation control surface are
  incomplete.
- Success-metric targets are listed in the PRD but are not yet backed by a
  durable evaluation harness and historical measurements.
- The native iOS project has not been generated, signed, compiled, or exercised
  on Apple hardware in this Windows environment.

## Provider Requirement

The owner's provider requirement is implemented:

- Local: Ollama
- API: OpenAI Responses-compatible providers
- API: Anthropic Messages
- Secrets: referenced by Vault name and injected only at provider/tool boundary
- Streaming: native SSE/NDJSON parsing for all three provider families

This remains subject to live credential and network verification with the
owner's selected accounts; automated tests use simulated transports and never
send owner data to external providers.

## Recommended Delivery Order

1. Apple release gate: generate/build/sign the Xcode project, run native tests,
   and validate biometrics, APNs, widget/share/background behavior, and offline recovery.
2. Phase 3 orchestration: workflow DAG, concurrent agents, checkpoints,
   cancellation, progress stream, playbooks, and routine rollback.
3. Connectors and production build pipeline.
4. Phase 4 proactive engine, plugin system, adaptive model routing, packaging,
   and whole-product evaluation/security gates.
