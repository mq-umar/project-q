# Project Q Phase 1 Gap Closure Design

## Scope

This design closes the three gaps found by auditing the April 2026 PRD against
the current implementation:

1. Voice input and output are one-shot rather than streaming.
2. Web research exposes browser primitives but no multi-source research
   workflow with source provenance.
3. Shell execution is scoped to approved directories but is not an isolated
   sandbox.

The remaining Phase 1 requirements already have working implementations and
regression coverage: text chat, episodic and semantic memory, filesystem and
Playwright automation, email/calendar read and draft paths, task planning and
tracking, audit logging, DPAPI secrets, and Research/Coding agent spawning.

## Selected Approach

### Streaming voice

Use the browser's native `SpeechRecognition` implementation on Edge/Chrome for
continuous recognition and interim transcript display. Final transcript
segments populate the composer and can be submitted through the existing
native SSE chat endpoint. While response tokens arrive, sentence-sized chunks
are queued through `speechSynthesis`, which provides low-latency spoken output
without adding a cloud voice dependency.

The existing Windows SAPI `voice.listen_once` and `voice.speak` tools remain as
the compatibility fallback. Starting microphone capture cancels current speech
output, so the owner can interrupt a response.

Alternatives rejected:

- A new server-side audio streaming protocol would require codec handling,
  microphone transport, and another STT runtime before it improved the current
  Windows MVP.
- Cloud-only speech would violate the local-first requirement and make voice
  unusable without an API key.

### Multi-source web research

Add a bounded `research.web` tool and `WebResearchService`.

The service:

1. Accepts a query, optional seed URLs, and a source limit.
2. Uses Playwright inspection to collect public search-result links when seed
   URLs are not supplied.
3. Validates each URL, deduplicates domains, and inspects a bounded number of
   sources.
4. Scans every extracted page through the existing Zone 3 prompt-injection
   defense before synthesis.
5. Returns a structured source packet containing title, URL, excerpt, trust
   findings, and retrieval errors.
6. Produces a deterministic cross-source digest even when no model is enabled.
   When a provider is enabled, the normal reasoner can use the source packet;
   source provenance remains in the tool result and audit record.

The local intent router will map explicit research requests without supplied
URLs to `research.web`. Existing one-page inspection behavior remains intact
for direct URL summaries.

Alternatives rejected:

- Treating a Google results page as "multi-source research" does not inspect or
  compare the underlying sources.
- Requiring a paid search API would make the Phase 1 path unavailable by
  default.

### Sandboxed shell execution

Split shell execution into two explicit backends:

- `sandbox_first`: run in a Docker container with no network, bounded memory
  and CPU, a non-root user, dropped Linux capabilities, and only the approved
  working directory mounted into `/workspace`.
- `direct_trusted`: run PowerShell directly in an approved directory. This mode
  is never reported as sandboxed and still requires the existing Tier 2 policy
  approval.

If `sandbox_first` is selected and Docker is unavailable, the command fails
closed with a useful setup error. It does not silently execute on the host.
The tool result records backend, isolation status, timeout, and command result.

Alternatives rejected:

- A working-directory restriction is not a security sandbox.
- PowerShell constrained language mode is useful hardening but not a reliable
  boundary against a process already running as the owner.
- Silently falling back to direct execution would contradict the setting and
  the audit log.

## Security Boundaries

- Research follows the existing HTTP(S)-only URL policy and Zone 3 origin
  tagging. Search-result redirects are normalized before inspection.
- Research content cannot authorize tool calls or owner approval.
- Streaming voice requires `voice_enabled`; browser permission remains under
  owner control.
- Speech text is never written to the audit log verbatim; only transcript
  length and resulting object identifiers are recorded.
- Sandbox mode mounts one approved directory, disables networking, drops
  capabilities, applies resource limits, and runs as a non-root user.
- Direct shell mode remains visibly distinct in settings, result payloads, and
  audit records.

## Testing

- Unit tests for research link selection, domain diversity, trust scanning,
  partial source failures, and deterministic synthesis.
- Reasoner routing and conversation execution tests for `research.web`.
- Shell command-construction tests proving network isolation, resource limits,
  non-root execution, and fail-closed behavior.
- Direct-mode regression tests proving approved-root enforcement remains.
- Static frontend tests for continuous/interim speech recognition,
  interruption, SSE speech chunking, and SAPI fallback wiring.
- End-to-end simulations for research, shell backend selection, voice
  transcript ingestion, provider/local chat, audit records, and secret
  non-disclosure.

## Completion Criteria

Phase 1 is complete only when:

1. The verifier checks runtime behavior rather than counting registered tools.
2. Every PRD Phase 1 line maps to a passing test or an explicit environment
   prerequisite.
3. The full Python suite, JavaScript syntax check, compile check, security
   simulations, and isolated Phase 1 verifier pass.
4. Documentation no longer describes workspace scoping as sandbox isolation or
   one-page browser inspection as multi-source research.
