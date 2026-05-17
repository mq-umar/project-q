# Project Q Architecture

## MVP Shape

The initial build is intentionally Windows-first and local-first.

### Core layers

1. `Desktop UI`
   A static single-page application served by the local backend. It provides the conversational shell, task queue, memory browser, agent monitor, audit viewer, and settings panel.
2. `Application Server`
   A lightweight Python HTTP server that exposes JSON endpoints, assembles context, routes chat interactions, enforces policy, and logs all consequential actions.
3. `Service Layer`
   Focused services handle conversations, context assembly, reasoning, plan execution, agent runs, routine runs, memories, tasks, agents, routines, settings, vault access, and audit events.
4. `Tool Workers`
   The first tool workers are filesystem, shell, Playwright browser primitives, and Windows operator primitives for app launch, open-window discovery, window activation, and keystrokes. They are intentionally bounded by policy and workspace rules.
5. `Persistence`
   SQLite stores operational state. Secrets are stored separately and encrypted via DPAPI on Windows.

## Security Model

- All user-facing actions are recorded in the audit log.
- Tools are assigned an action tier.
- Tier 0 and Tier 1 tools may run automatically depending on settings.
- Tier 2 and Tier 3 tools require explicit approval unless the owner later defines a trusted routine.
- Secrets never appear in logs, prompts, or memory records.

## Reasoning Layer

- `ContextService` assembles recent messages, task state, memories, agents, and available tools.
- `ContextService` now also includes routines so the model can build on reusable automations.
- `ReasonerService` supports built-in heuristic planning, free local-model planning through Ollama-compatible chat endpoints, and OpenAI-compatible remote planning.
- Local Ollama model discovery is supported through the `/api/tags` endpoint.
- The local-first stack now supports a routed four-model Ollama setup: `qwen3.5:9b` for general work, `qwen2.5-coder:7b` for coding, `deepseek-r1:7b` for reasoning, and `llama3.1:8b` for fast turns.
- Conversation turns can now create tasks, memories, agents, routines, and auto-execute allowed tools in a single pass.
- `PlanExecutorService` applies a reasoner plan consistently across conversations and agent runs.
- `AgentRunnerService` gives agents a first execution loop with run history, audit logging, and latest-run summaries.
- `RoutineRunnerService` gives trusted routines a reusable execution path with step-level results, approval gating, and run history.

## Desktop UI Modules

- `Chat`
- `Tasks`
- `Memories`
- `Agents`
- `Routines`
- `Audit`
- `Settings`

## iPhone Companion

The iPhone folder currently contains SwiftUI scaffolding for:

- live status dashboard
- approvals
- quick capture
- task and memory browsing
- future secure session handoff

## Next Build Priorities

1. Expand Windows automation from operator primitives into richer UI Automation and screen context
2. Add voice loop
3. Add encrypted relay for iPhone
4. Add real connector integrations
5. Expand agent orchestration into longer-running multi-step work
