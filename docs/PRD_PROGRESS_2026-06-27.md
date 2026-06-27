# Project Q — PRD Progress Log (week of 2026-06-21 → 06-27)

All work committed + pushed to `mq-umar/project-q.git` on `codex/phase3-orchestration`.
Each track was built and adversarially reviewed, then gated on a full unittest run.

| Commit | Track | What landed |
|--------|-------|-------------|
| `3f658d5` | **Security & reliability audit** | 43-finding multi-agent audit; ~30 fixed (owner-auth hardening + optional passphrase, Zone-3 trust-envelope provenance, Tier-3 auto-approval lockout, §12.1 profiles enforced, vault scheme tagging, workflow orphan-run/retention, LoRA→Ollama serving switch, §6.2 reflection). See `docs/AUDIT_RESPONSE_2026-06-21.md`. |
| `336da66` | **Memory depth** (PRD §6.1/§8) | Ollama embedding hook + hybrid FTS5 rerank (silent fallback), source-memory columns, owner-promotable procedural playbooks. |
| `1a306d7` | **Plugin/skill system** (Phase 4) | Owner-installable declarative HTTP/shell manifests (no arbitrary code), tier-gated, hot-registered. Security-hardened (no SSRF-via-redirect, no template traversal, path-traversal/reserved-name rejection). |
| `cb8f280` | **Connectors framework + GitHub/Slack** (§5.7/§10.4) | Bearer-token client (no-redirect, injectable transport, vault tokens, never raises). Tier-mapped; GitHub create-issue raised to tier 2; repo path-injection hardened. |
| `7984909` | **Connectors: Notion + Todoist** | Search/create-page + list/create-task, reusing the framework. |
| `f71a2b0` | **Dashboard wiring** | `/api/memories/playbooks` list + promote routes; Plugins / Playbooks / Connector-readiness UI panels. |

## Test baseline
Full `unittest discover` grew from **318 → 387** tests, green each track (a few live-HTTP
streaming/relay tests flake only under full-suite parallel load; they pass in isolation).

## Remaining PRD gaps (next candidates)
- **In-env, additive:** more connectors via non-bearer auth (Linear raw-key/GraphQL, Jira basic-auth); agent-supervisor root-budget propagation (§7); screenshot/audio auto-expiry (§9.6).
- **Dependency-gated:** AES-256 at-rest (SQLCipher); neural embeddings beyond Ollama (LanceDB).
- **Credential-gated:** OAuth connectors (Google/Gmail/Drive/Calendar) — need the owner's OAuth app.
- **External/release gates:** iOS device build + APNs (macOS/Xcode); wake word (Porcupine + mic); MSIX shell.

How to use the new connectors: store the token in the **Vault** under the fixed secret
name (`github_token`, `slack_bot_token`, `notion_token`, `todoist_token`), then call the
`github.*` / `slack.* `/ `notion.* `/ `todoist.*` tools (tier-gated) from the Tools panel or an agent.
