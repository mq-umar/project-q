# Project Q Browser Automation Audit - 2026-06-11

## Scope

Reviewed the Python browser tools, Playwright worker, runtime discovery,
reasoner routing, settings, setup script, Phase 1 verifier, and browser security
boundaries.

## Fixed Findings

1. The bundled `playwright` package could not load `playwright-core`.
   Project Q now includes the bundled pnpm dependency directory in `NODE_PATH`.
2. Browser verification mocked `subprocess.run`, so it passed when no browser
   could launch. The verifier now launches the installed browser and performs
   real offline interactions.
3. Controlled actions were headless and disposable by default. Actions now use
   a visible, isolated Project Q profile and persist cookies/site state across
   browser restarts without sharing the owner's personal browser profile.
4. Interactive model plans were rewritten to URL-only browser opens. Click,
   fill, submit, login, and download requests now preserve
   `browser.run_actions` plans when a configured model supplies exact steps.
5. Nested action URLs were not validated before browser launch. Initial,
   `goto`, and `new_tab` URLs now receive the same public HTTP(S) validation.
6. Only navigation requests were checked in the worker, allowing private
   subresource requests. Every HTTP(S) request is now checked.
7. Service workers and WebSockets could bypass normal request routing. Service
   workers are blocked and WebSocket targets receive public-host validation.
8. Screenshot, profile, and download locations are confined beneath
   `.project_q`; profile names and action counts are bounded.
9. File uploads are limited to existing files under the workspace, Project Q
   data root, or an owner-configured allowed root, with a 100 MiB limit.
10. Owner interaction waits could outlive the Python subprocess timeout.
   `pause_for_owner` now expands the bounded worker timeout automatically.
11. The setup script omitted the pnpm dependency path and hardcoded one user
    name. It now uses `%USERPROFILE%` and the complete `NODE_PATH`.

## Verification

- Real offline self-test: launch, fill, press, select, check/uncheck, hover,
  click, extraction, wait, download, tab switch/close, screenshot, persistent
  profile reopen, private HTTP blocking, and private WebSocket blocking.
- Real public simulation: `example.com` click navigation to IANA, second-tab
  navigation to `example.org`, tab switching, extraction, and screenshots.
- Real visible-mode simulation using the saved Project Q settings.
- Browser-focused unit suite and the complete Phase 1 behavioral verifier.

## Residual Limits

- Browser actions control an isolated Project Q browser profile, not an already
  open personal Edge/Chrome profile.
- DNS validation reduces SSRF risk but cannot provide network-layer IP pinning
  against every possible DNS rebinding race. Do not expose the local control
  service to untrusted networks.
- CAPTCHAs, hardware security keys, and some anti-automation sites may require
  `pause_for_owner` and manual completion.
