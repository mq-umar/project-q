# Project Q — PRD Progress Log (2026-06-21 → 06-27)

All work committed + pushed to `mq-umar/project-q.git` on `codex/phase3-orchestration`.
Each track was built (most via a design → implement → adversarial-review multi-agent
workflow) and gated on a full unittest run before commit.

| Commit | Track |
|--------|-------|
| `3f658d5` | **Security & reliability audit** — 43 confirmed findings, ~30 fixed (owner-auth hardening + optional passphrase, Zone-3 trust-envelope provenance, Tier-3 auto-approval lockout, §12.1 profiles enforced, vault scheme tags, workflow orphan-run/retention, LoRA→Ollama serving, §6.2 reflection). See `AUDIT_RESPONSE_2026-06-21.md`. |
| `336da66` | **Memory depth** (§6.1/§8) — Ollama embeddings + hybrid FTS5 rerank (silent fallback), source-memory fields, owner-promotable procedural playbooks. |
| `1a306d7` | **Plugin/skill system** (Phase 4) — declarative HTTP/shell manifests, tier-gated, hot-registered; security-hardened. |
| `cb8f280` | **Connectors framework + GitHub/Slack** (§5.7/§10.4) — bearer-token client (no-redirect, injectable transport, vault tokens, never raises). |
| `7984909` | **Connectors: Notion + Todoist** |
| `f71a2b0` | **Dashboard wiring** — playbook promote routes + Plugins/Playbooks/Connectors UI panels. |
| `474f8b0` | **Docs** — README + this progress log. |
| `66da44e` | **Connectors: auth-scheme generalization + Linear** (raw-key GraphQL). |
| `2a33cef` | **Connectors: Google OAuth2** (refresh-token) — Calendar + Drive. |
| `94182f9` | **Connectors: Microsoft Graph** — Outlook mail/calendar, OneDrive, send-mail. |
| `aa8d706` | **Artifact retention** (§9.6) — screenshot/OCR auto-expiry. |
| `c20931e` | **Agent supervisor** (§7) — root tool-call budget propagation across a workflow. |
| `cb57308` | **MCP client** (Claude-Code-style external tools) — clean-room JSON-RPC/stdio, isolated reader-thread + timeouts, owner-gated install/remove, never breaks startup. |
| `0f0a382` | **Get-smarter loop closed + ceilings raised** (23-agent assessment) — reflect-in-cycle drives playbook candidates; recurrence-ranked candidates; dropped the 3-tool truncation (default 8 / honor agent budget); research.web on the Research template; coherent non-TODO script projects. |
| `295ec72` | **Observe-then-replan** — gated, synthesis-only refine pass feeds read-only tool observations back to the provider so any model answers grounded in tool output; never re-executes tools. Dashboard toggle + smartness note. |
| `ed1db89` | **Capability verification + spreadsheet formulas** — `tests/_capability_e2e.py` exercises 22 capabilities end-to-end against a live instance (22/22 PASS); `spreadsheet.analyze` gains a whitelisted-AST derived cross-column `expression` (e.g. `revenue - cost`), additive + no code-exec surface. |

## Connector coverage (PRD §10.4)
GitHub · Slack · Notion · Todoist · Linear · Google (Calendar/Drive, OAuth2) ·
Microsoft Graph (Outlook mail/calendar, OneDrive). Tokens live in the **Vault**
under fixed secret names; tier-mapped per the PRD table; all credential-free testable.

## Test baseline
Full `unittest discover` grew **318 → 467** tests, green each track (MCP +26, capability
assessment fixes, observe-then-replan +7, spreadsheet formulas +7). Plus a standalone
**22/22 end-to-end capability probe** (`tests/_capability_e2e.py`, run explicitly). Runtime
is ~10-12 min — the suite spins real HTTP servers, subprocesses, and PBKDF2/crypto, so it
is slow, not hung.

## What remains — all hard-gated, not buildable/verifiable in this environment
- **AES-256 full-DB at-rest** (§9.6): needs the SQLCipher dependency. Documented as the
  upgrade path; secrets are already DPAPI-encrypted. (Not shipped as unverifiable code.)
- **OAuth connectors UX**: Google/Microsoft connectors are built; they activate once the
  owner completes the one-time OAuth consent and stores the refresh token in the vault.
- **iOS device build + live APNs**: requires macOS/Xcode + Apple credentials + a device.
- **Wake word / always-on listening** (§10.2): requires mic hardware + Porcupine.
- **Packaged MSIX desktop shell** (§10.1): requires Windows packaging tooling.
