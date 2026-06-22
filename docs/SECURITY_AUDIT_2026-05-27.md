# Project Q Security And Functionality Audit - 2026-05-27

## Scope

This audit covered the current Windows-local Project Q repository: Python server, service layer, local owner sessions, tool registry, model-provider routing, emergency control plane, companion pairing/envelope layer, voice transcript/TTS path, filesystem management tools, browser and Windows tools, generated artifact paths, static UI, iPhone companion scaffold, tests, and the PRD requirements in `Project_Q_PRD.docx`.

The PRD is a multi-phase product. The current repo is a functioning local MVP foundation, not the complete PRD: wake-word/background STT handling, continuous visual state understanding, real productivity connectors, encrypted iPhone relay, packaged MSIX, and full iOS integration remain future work.

## Threat Model Summary

Primary assets are local files, owner memory/tasks/conversations, audit history, API keys, model-provider requests, tool execution authority, and generated code/artifacts. Important trust boundaries are owner commands, external web content, local browser pages, remote/local model output, the Vault, filesystem roots, shell/Windows tools, and the local HTTP control surface.

Security invariants:

- Vault secrets must not be inserted into model prompts or logs.
- Remote or local model output can suggest actions, but tool execution must pass policy.
- External content must not become authority for privileged tools.
- File and artifact writes must stay inside approved roots.
- Local HTTP access must stay local-owner oriented, not browser-rebindable.

## Findings Fixed

1. Anthropic provider support was missing.
   Fixed by adding `anthropic_messages` provider routing using Anthropic's Messages API shape: `model`, `max_tokens`, top-level `system`, `messages`, `x-api-key`, `anthropic-version`, and JSON response text extraction.

2. Browser tools accepted non-web URLs.
   Fixed by rejecting non-HTTP(S) URLs before Playwright launches, preventing `file://` reads through the auto-approved browser inspection path.

3. Browser screenshot artifact names could escape the artifact directory.
   Fixed by requiring screenshot names to be simple filenames and checking the resolved path remains under `.project_q/browser_artifacts`.

4. `windows.open_url` accepted arbitrary protocols.
   Fixed by limiting it to HTTP(S), keeping local file/application opening on the audited filesystem/Windows tool paths.

5. Local HTTP server lacked Host/Origin guard helpers.
   Fixed with localhost/loopback Host validation and Origin validation for mutating requests, reducing DNS-rebinding and cross-origin local-control risk.

6. Generated script prompts could break Python/JS strings.
   Fixed by using JSON string escaping semantics for generated Python and JavaScript string literals.

7. No emergency stop existed for autonomous/background work.
   Fixed by adding a kill switch control plane that stops the Learning Lab, blocks new mutating API calls and tool authorizations, exposes dashboard and iPhone companion controls, and records activate/resume events in the audit log.

8. No iPhone pairing or signed companion request path existed.
   Fixed by adding one-time companion pairing, Vault-stored shared keys, HMAC-signed companion requests, encrypted quick-capture envelopes, origin-tagged phone memories, device listing/revocation, and matching Swift API helpers.

9. Local mutating API routes did not require an owner session.
   Fixed by adding owner-session issuance for the dashboard, hashed session storage, mutating route enforcement, and explicit exemptions only for emergency stop and signed companion quick-capture/pairing completion.

10. Windows screen/clipboard context was missing from the operator layer.
    Fixed by adding STA clipboard read/write tools and virtual-screen screenshot capture with artifact path confinement under `.project_q/windows_artifacts`.

11. Prompt-injection defenses lacked a dedicated scanner and red-team regression.
    Fixed by adding `TrustBoundaryService`, the tier-0 `security.scan_external_content` tool, external-source policy checks that prevent untrusted content from acting as owner approval, safe context wrapping, and a diagnostics red-team simulation.

12. Voice had a setting but no functioning local path.
    Fixed by adding a Windows text-to-speech tool gated by `voice_enabled`, audited transcript ingestion through the conversation pipeline, an owner-session-protected voice transcript API, dashboard setting/tool payloads, and regression tests.

13. Filesystem automation lacked move/rename and zip archive operations from the PRD.
    Fixed by adding `filesystem.move_path` and `filesystem.create_zip` with allowed-root enforcement, no implicit overwrite, relative archive entries, dashboard payloads, model-routing guidance, and regression tests.

14. Windows automation lacked native UI Automation inspection/invocation.
    Fixed by adding `windows.inspect_ui_tree` for bounded UIA element discovery and `windows.invoke_ui_element` for explicit InvokePattern actions after owner approval, plus dashboard payloads, model-routing guidance, diagnostics registration, and regression tests.

15. Registry access from the PRD was missing.
    Fixed by adding scoped `windows.registry_read` and Tier 3 `windows.registry_write`, limiting writes to `HKCU:\Software\ProjectQ`, using encoded PowerShell, registering diagnostics/tool payloads, and adding regression tests for scope enforcement.

16. File watching from the PRD was missing.
    Fixed by adding snapshot-based `filesystem.watch_start` and `filesystem.watch_poll` tools that operate only inside allowed roots and report added, modified, and deleted files without reading file contents into logs.

17. Windows notification dispatch from the PRD was missing.
    Fixed by adding `windows.notify`, a Tier 1 Windows toast notification tool gated by `notifications_enabled`, with encoded PowerShell execution, dashboard payloads, diagnostics registration, and regression tests.

18. OCR-backed screen text extraction from the PRD was missing.
    Fixed by adding `windows.ocr_screenshot`, a Tier 1 Windows OCR tool that reads only `.project_q/windows_artifacts` screenshot PNGs or captures a fresh confined artifact, uses STA encoded PowerShell, is registered in diagnostics and dashboard payloads, and has regression tests for OCR output handling and path confinement.

19. Browser inspection/action tools could be pointed at direct localhost or private-network URLs.
    Fixed by rejecting loopback, `.localhost`, private, link-local, unspecified, reserved, and multicast direct hosts before Playwright launch, with regression coverage for inspect/action tools.

20. Live local voice input was limited to already-transcribed text.
    Fixed by adding `voice.listen_once`, a Tier 1 Windows speech-recognition tool gated by `voice_enabled`, using encoded PowerShell and returning one microphone utterance with source metadata. Background listening and wake-word activation remain future work.

21. Memory controls lacked PRD-required export and scoped bulk deletion.
    Fixed by adding versioned provenance-preserving memory export, dry-run bulk deletion by source/kind/tag/age/all-memory confirmation, owner-session-protected API endpoints, audit entries, and regression tests for confirmation and session enforcement.

22. File overwrites lacked a rollback snapshot trail.
    Fixed by adding pre-overwrite snapshots for `filesystem.write_file` under `.project_q/file_snapshots`, returning snapshot metadata in the tool result, and adding regression coverage that verifies old content is recoverable.

23. Communication/calendar outputs had no safe local draft path before real connectors.
    Fixed by adding `communications.email_draft` for local RFC 822 `.eml` drafts with header-injection checks and `calendar.create_invite` for local `.ics` files, both Tier 1 owner-review artifacts with dashboard payloads, diagnostics registration, and regression tests.

## Verification

- Unit suite: 123 tests passing.
- Python compile: `compileall -q src` passing.
- UI syntax: `node --check src/project_q/static/app.js` passing.
- Runtime simulation: diagnostics passed, Learning Lab completed, and Anthropic chat routing produced a remote-mode task with a mocked provider response.

## Residual Risks And PRD Gaps

- Local owner sessions are implemented, but they are not yet backed by Windows Hello or a PIN challenge.
- The kill switch now blocks new Project Q actions, but it is not yet wired to terminate external child processes that are already running outside the current service loops.
- The companion envelope layer uses a standard-library HMAC-derived stream envelope because no vetted crypto package is installed in the current Python runtime. It is useful for local simulation and relay-shape testing, but should be replaced with platform-grade primitives before exposing the relay over the internet.
- SQLite application data is not encrypted at rest; Vault secrets use DPAPI on Windows, but the broader PRD encryption goal is not complete.
- Memory export/delete controls are available, but memory import and configurable auto-expiry are still future work.
- Shell execution now defaults to a fail-closed Docker sandbox with no network, resource limits, dropped capabilities, `no-new-privileges`, and non-root execution. Explicit `direct_trusted` mode remains a higher-risk owner choice.
- Registry write exists but is intentionally limited to Project Q's HKCU registry scope until a broader owner-reviewed registry policy is added.
- File watching is currently polling/snapshot based rather than a persistent OS event subscription.
- Browser HTTP(S) inspection resolves initial hostnames before launch and the
  Playwright worker rechecks every navigation request, including redirects,
  blocking localhost and private/link-local address targets.
- Prompt-injection defenses now have heuristic scanning and regression coverage, but future mail/browser/document connector loops must consistently route external content through the scanner before feeding it to planning prompts.
- Voice now has continuous/interim browser STT, streamed browser TTS with interruption, Windows SAPI fallback, and audited transcript ingestion. Wake-word activation and background listening remain unimplemented.
- Real email/calendar/drive/chat sync/send connectors, continuous visual state understanding, packaged desktop shell, APNs, and the off-network relay service are not implemented in this repo yet.

## Sources Checked For Provider Shape

- Anthropic API overview and Messages API examples: https://docs.anthropic.com/en/api/overview and https://docs.anthropic.com/en/api/messages-examples
- OpenAI API authentication/reference context: https://platform.openai.com/docs/api-reference
