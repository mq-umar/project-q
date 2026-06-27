# Project Q — What You (the Owner) Need To Do

Everything buildable + testable in this environment is **done, green, and pushed**
(branch `codex/phase3-orchestration` on `mq-umar/project-q.git`). The items below
are the things only *you* (or a Mac / a dependency / a credential) can complete.
Ordered by effort.

## 1. Run it (no setup needed)
```powershell
cd "C:\Users\umarq.APEX\Documents\Jarvis\.worktrees\phase3-orchestration"
.\launch_project_q.ps1            # serves http://127.0.0.1:8787
```
Open that URL in Edge/Chrome. Runs in local-heuristic mode out of the box.
Run the tests anytime: `$env:PYTHONPATH='src'; & <python> -m unittest discover -s tests`.

## 2. Turn on real AI chat (optional)
Settings → Provider. Free/local: install [Ollama](https://ollama.com), pull a model
(e.g. `qwen2.5-coder:7b`), set provider=`ollama`. Or set provider=`openai`/`anthropic`
and store the API key in the **Vault** (Secrets panel). Ollama also powers the new
**semantic memory** embeddings automatically once it's running.

## 3. Activate connectors — store a token in the Vault
Open the **Connectors** panel (it shows which are configured). Add each token in the
**Secrets/Vault** panel under the exact secret name, then call the tool from the Tools panel or an agent:

| Service | Vault secret name(s) | How to get it |
|---|---|---|
| GitHub | `github_token` | github.com → Settings → Developer settings → Personal access token |
| Slack | `slack_bot_token` | api.slack.com → your app → Bot token (`xoxb-…`) |
| Notion | `notion_token` | notion.so → My integrations → Internal integration token |
| Todoist | `todoist_token` | todoist.com → Settings → Integrations → API token |
| Linear | `linear_api_key` | linear.app → Settings → API → Personal API key |
| **Google** (Calendar/Drive) | `google_client_id`, `google_client_secret`, `google_refresh_token` | see §4 |
| **Microsoft** (Outlook/OneDrive) | `ms_client_id`, `ms_client_secret`, `ms_refresh_token` | see §4 |

## 4. One-time OAuth consent for Google / Microsoft
The connectors are built; they need a long-lived **refresh token** you generate once:
1. Create an OAuth app (Google Cloud Console / Azure App Registration). Scopes:
   Google → Calendar + Drive; Microsoft → `Mail.Send Mail.Read Calendars.Read Files.Read offline_access`.
2. Do the one-time consent flow (any standard OAuth helper / the provider's playground)
   to obtain a **refresh token**.
3. Store the `client_id`, `client_secret`, and `refresh_token` in the Vault under the
   names in §3. The connectors then mint short-lived access tokens automatically.

## 5. Full-database encryption at rest (PRD §9.6) — install one dependency
Secrets are already DPAPI-encrypted; the rest of the SQLite DB is plaintext. To enable
AES-256 at-rest, install **SQLCipher**: `pip install pysqlcipher3` into the runtime.
Then ping me and I'll wire `Database` to open via SQLCipher with a DPAPI-sealed key +
add the tests — it was intentionally not shipped as unverifiable code.

## 6. iPhone companion — needs a Mac
The iOS app + relay are source-complete. Building/signing/testing requires **macOS +
Xcode**, your **Apple Developer** signing, **APNs** credentials, and a **physical device**.
(Out of scope for a Windows environment.)

## 7. Wake word & MSIX shell — hardware / packaging
- Wake word / always-on listening (§10.2): needs a **microphone** + a wake-word engine
  (e.g. Picovoice Porcupine).
- Packaged **MSIX** desktop shell (§10.1): needs the Windows app-packaging toolchain.

## 8. Recommended hardening if this PC is ever shared
Owner login is loopback-trust by default. To require a passphrase:
`POST /api/auth/passphrase {"passphrase": "…"}` (while you have a session). After that,
new sessions require `POST /api/auth/login`.

---
**Status:** Phase 1 done · Phase 2 source-complete (needs §6) · Phase 3 ~90% · Phase 4 ~60%.
Two full audits (53 + 10 confirmed findings, all adversarially verified, all fixed),
421 unit tests + Phase-1/2 verifiers + prompt-injection & self-diagnostics simulations —
all green. Per-commit log: `docs/PRD_PROGRESS_2026-06-27.md`.
