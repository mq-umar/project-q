# Provider-Native Streaming Design

## Goal

Replace Project Q's simulated word-by-word chat stream with real incremental output from Ollama, OpenAI Responses, and Anthropic Messages while preserving structured planning, policy checks, tool execution, persistence, and provider fallback behavior.

## Scope

This change covers the existing `POST /api/chat/stream` path. The non-streaming `POST /api/chat` path remains compatible and continues to use the existing complete-response provider transport.

The implementation does not add a second model request. Each provider still produces one structured `ReasonerPlan`; Project Q incrementally reveals only its top-level `reply` string while buffering the full structured document for validation and execution.

## Architecture

### Provider stream adapter

Create `src/project_q/services/provider_streaming.py` with:

- A standard-library HTTP streaming transport that yields response lines without buffering the whole body.
- Provider-specific event parsers:
  - OpenAI Responses SSE: `response.output_text.delta`
  - Anthropic Messages SSE: `content_block_delta` with `text_delta`
  - Ollama chat NDJSON: `message.content`
- Explicit handling for provider error events and malformed stream records.
- An incremental JSON string-field decoder that exposes decoded characters from the top-level `reply` field only.

The adapter must ignore unknown forward-compatible SSE events and must never emit reasoning fields, tool payloads, memory writes, or other plan JSON.

### Reasoner streaming

Add a streaming path to `ReasonerService` that reuses the current intent normalization, deterministic routing, model selection, provider configuration, plan normalization, auditing, no-op override, and local fallback rules.

For a configured provider:

1. Send the existing structured planning prompt with provider streaming enabled.
2. Accumulate the provider's text deltas as one JSON document.
3. Feed each text delta into the incremental `reply` decoder.
4. Yield decoded reply deltas to the conversation layer.
5. Parse and validate the complete document as `ReasonerPlan`.
6. Return the final `ReasonerResult`.

For deterministic, heuristic, or provider-fallback paths, return the final result without manufacturing delayed word tokens.

### Conversation streaming

Add `ConversationService.respond_stream()` as the single owner of a streaming chat turn:

1. Persist the owner message.
2. Build context.
3. Forward safe reply deltas from the reasoner.
4. Wait for a validated final plan.
5. Execute plan writes and tools through `PlanExecutorService`.
6. Compose the canonical final reply.
7. Stream any canonical suffix not already emitted, such as tool results or warnings.
8. Persist the canonical assistant reply.
9. Yield the final `ChatResponse`.

No side effect occurs from partial provider output.

### HTTP SSE layer

`ProjectQHandler._stream_chat()` becomes a thin adapter over `ConversationService.respond_stream()`:

- Send `token` events as soon as they arrive.
- Send one final `done` event containing the canonical response metadata.
- Send a structured `error` event if an unexpected failure occurs after SSE headers are committed.
- Remove artificial sleeps.
- Add `X-Accel-Buffering: no` and connection-friendly cache headers.

The frontend's existing `token` and `done` handling remains compatible. The `done.reply` value remains authoritative and replaces any partial display if fallback or normalization changed the provider reply.

## Provider Protocols

### OpenAI Responses

The request adds `"stream": true`. The direct HTTP response is SSE. Text is accumulated from events whose JSON `type` is `response.output_text.delta`; the `delta` field contains the next text fragment. Provider `error` and failed/incomplete terminal events raise a controlled stream error.

### Anthropic Messages

The request adds `"stream": true`. The direct HTTP response is SSE. Text is accumulated from `content_block_delta` events where `delta.type` is `text_delta`; `delta.text` contains the next fragment. `error` events raise a controlled stream error.

### Ollama

The request uses `"stream": true`. The direct HTTP response is newline-delimited JSON. Text is accumulated from each record's `message.content`. A record containing `error` raises a controlled stream error.

## Incremental JSON Safety

The decoder recognizes a top-level JSON property named exactly `reply` and decodes its JSON string incrementally, including escaped quotes, backslashes, control escapes, and Unicode escape sequences.

It emits nothing before the `reply` string begins and stops permanently at that string's closing quote. It does not use regular expressions to parse JSON and does not expose text from any other field.

The full accumulated provider text is still parsed with the existing JSON extraction logic and validated by Pydantic. A valid-looking partial reply never authorizes execution.

## Failure Behavior

- Provider fails before or during streaming: audit the provider failure and use the existing local fallback plan.
- Provider emits partial reply and later fails: the frontend may briefly show the partial text; the final `done.reply` replaces it with the fallback canonical reply.
- Client disconnects: stop writing SSE immediately. The generator is closed so an in-flight provider HTTP response can be released.
- Tool execution fails: send an SSE error event and audit through existing server/tool paths; do not claim a completed turn.
- Malformed provider JSON: no plan side effects occur; use local fallback.

## Tests

Add focused tests for:

- Incremental `reply` decoding across arbitrary chunk boundaries and JSON escapes.
- OpenAI, Anthropic, and Ollama event parsing, including provider error records.
- Provider request payloads include streaming flags and credentials remain headers only.
- Reasoner streams only reply text and returns a validated plan.
- Invalid streamed plan falls back without executing plan writes.
- Conversation streams provider deltas, then tool-result suffix, and persists the canonical reply once.
- HTTP SSE sends provider-sized deltas without artificial sleep and ends with canonical metadata.
- Existing non-streaming provider and chat tests remain green.

## Security

- Vault secrets stay in request headers and are never included in SSE events, prompts, audit metadata, or conversation rows.
- Only the decoded `reply` field can reach the browser before full validation.
- Structured plan fields remain subject to Pydantic validation, policy authorization, trust-zone rules, and existing execution limits.
- Streaming transport uses configured timeouts and bounded provider output accumulation.
- Unknown provider events are ignored; explicit provider errors fail closed into local fallback.

## Completion Evidence

Completion requires:

- Focused streaming tests for all three providers.
- Full Python unit suite.
- Python compilation.
- JavaScript syntax validation.
- `git diff --check`.
- Live ephemeral HTTP SSE simulation proving multiple real transport deltas arrive before the final event and no internal plan JSON is exposed.
