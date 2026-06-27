# Third-Party Notices

## Jarvis Voice AI Assistant

Project Q includes a vendored copy of `jarvis-main` under `third_party/jarvis-main` for personal, non-commercial reference and integration work.

- Copyright: Copyright (c) 2026 Ethan Rogers
- License: Personal, educational, and non-commercial use permitted with attribution.
- Commercial use: Prohibited without a commercial license from Ethan Rogers / ethanplus.ai.

The full license text is preserved at `third_party/jarvis-main/LICENSE`.

Integrated into Project Q (adapted to its architecture, not copied verbatim): proactive
post-build suggestions (`src/project_q/services/suggestions.py`), A/B experiment tracking
(`src/project_q/services/ab_testing.py`), and an audio-reactive voice orb
(`src/project_q/static/orb.js`). The macOS-only connectors (AppleScript Calendar / Mail /
Notes), the Claude-Code-CLI work-mode, and features already covered by Project Q were not
ported.

## Claude Code (NOT integrated)

A `claude-code-main` archive describing itself as leaked, not-for-redistribution proprietary
source belonging to Anthropic, PBC was reviewed and **deliberately not integrated** — copying
it would infringe Anthropic's copyright/trade secrets and violate its "NOT FOR REDISTRIBUTION"
license. Any desired capability is reimplemented from scratch on Project Q's own primitives.
