# Provider-Native Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream real incremental reply output from Ollama, OpenAI Responses, and Anthropic Messages through Project Q's existing chat SSE endpoint without exposing or executing unvalidated plan JSON.

**Architecture:** Add an isolated provider-stream parser and incremental top-level `reply` decoder, then layer streaming reasoner and conversation APIs over the existing structured-plan execution path. Keep `/api/chat` unchanged and make `/api/chat/stream` forward safe provider deltas followed by one canonical final response.

**Tech Stack:** Python 3.12 standard library `urllib`, SSE, NDJSON, Pydantic, unittest, static JavaScript frontend.

---

### Task 1: Provider Stream Parsing

**Files:**
- Create: `src/project_q/services/provider_streaming.py`
- Modify: `tests/test_project_q.py`

- [x] **Step 1: Add failing decoder tests**

Test a top-level JSON document split across awkward boundaries:

```python
extractor = IncrementalJsonReplyExtractor()
chunks = ['{"rep', 'ly":"Hello \\\\u263a and \\\\"quoted', '\\\\" text","tool_calls":[{"payload":"secret"}]}']
emitted = "".join(part for chunk in chunks for part in extractor.feed(chunk))
self.assertEqual(emitted, 'Hello \u263a and "quoted" text')
```

Also assert that content from `tool_calls`, `memory_writes`, and fields preceding `reply` is never emitted.

- [x] **Step 2: Run decoder tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
& 'C:/Users/umarq.APEX/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m unittest tests.test_project_q.ProjectQApplicationTests.test_incremental_reply_extractor_decodes_only_top_level_reply
```

Expected: import failure because `provider_streaming.py` does not exist.

- [x] **Step 3: Implement the incremental decoder**

Implement a state machine that:

- Locates a top-level `"reply"` key.
- Waits for its string value.
- Decodes JSON escapes incrementally.
- Emits decoded text only from that value.
- Stops after the closing quote.
- Enforces a bounded accumulated provider document size.

- [x] **Step 4: Add failing provider-event parser tests**

Use representative official protocol records:

```python
openai_lines = [
    'event: response.output_text.delta',
    'data: {"type":"response.output_text.delta","delta":"{\\"reply\\":\\"Hel"}',
    '',
]
anthropic_lines = [
    'event: content_block_delta',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo\\"}"}}',
    '',
]
ollama_lines = [
    '{"message":{"content":"{\\"reply\\":\\"Hello\\"}"},"done":false}',
    '{"message":{"content":""},"done":true}',
]
```

Assert exact text deltas, ignored unknown events, and controlled exceptions for provider error records.

- [x] **Step 5: Implement provider event parsing and streaming HTTP transport**

Add:

```python
def iter_provider_text(provider_type, lines) -> Iterator[str]: ...
def default_stream_transport(url, body, headers, timeout_seconds) -> Iterator[str]: ...
```

The HTTP transport must close its response when the iterator is closed.

- [x] **Step 6: Run Task 1 tests and verify GREEN**

Expected: all decoder/parser tests pass.

### Task 2: Streaming Reasoner

**Files:**
- Modify: `src/project_q/services/reasoner.py`
- Modify: `tests/test_project_q.py`

- [x] **Step 1: Add failing OpenAI streaming reasoner test**

Inject a fake stream transport that checks:

- `Authorization: Bearer ...`
- `"stream": true`
- API key absent from request JSON

Yield split `response.output_text.delta` events containing a complete structured plan. Assert reply deltas reconstruct only `plan.reply` and the final event contains a validated `ReasonerResult`.

- [x] **Step 2: Add equivalent Anthropic and Ollama tests**

Assert:

- Anthropic uses `x-api-key`, `anthropic-version`, and `"stream": true`.
- Ollama uses `"stream": true`, `format: "json"`, and the routed model.
- Internal plan JSON never appears in emitted reply deltas.

- [x] **Step 3: Run reasoner streaming tests and verify RED**

Expected: `ReasonerService` has no streaming API or stream transport.

- [x] **Step 4: Add streaming types and constructor dependency**

Add a small event dataclass:

```python
@dataclass(slots=True)
class ReasonerStreamEvent:
    token: str = ""
    result: ReasonerResult | None = None
```

Add an optional `stream_transport` dependency defaulting to `default_stream_transport`.

- [x] **Step 5: Implement provider request builders and streaming plan flow**

Reuse the exact non-streaming prompt and normalization behavior. Add `"stream": true` only on streaming requests. Accumulate bounded provider text, emit decoder output, validate the final `ReasonerPlan`, preserve auditing, no-op override, model selection, and local fallback.

- [x] **Step 6: Add invalid-plan fallback test**

Stream a visible partial reply followed by malformed plan JSON. Assert:

- Final result mode is `local-fallback`.
- No streamed structured field leaks.
- Provider failure is audited.
- No plan writes execute at the reasoner layer.

- [x] **Step 7: Run reasoner tests and verify GREEN**

Expected: all streaming and existing non-streaming reasoner tests pass.

### Task 3: Streaming Conversation Execution

**Files:**
- Modify: `src/project_q/services/conversation.py`
- Modify: `tests/test_project_q.py`

- [x] **Step 1: Add failing conversation streaming test**

Provide a streamed reasoner result with a reply and a Tier 0 tool call. Assert event order:

1. Provider reply token events.
2. Canonical suffix containing the tool result.
3. Final `ChatResponse`.

Assert the user and canonical assistant messages are each persisted once.

- [x] **Step 2: Run the test and verify RED**

Expected: `ConversationService.respond_stream` does not exist.

- [x] **Step 3: Implement `respond_stream`**

Share response finalization with `respond()` through private helpers so both paths execute the same policy and persistence behavior. Track already emitted reply text and emit only the canonical suffix when the final reply begins with it; otherwise let the final response replace partial UI text.

- [x] **Step 4: Add no-provider and fallback tests**

Assert deterministic/heuristic responses are emitted immediately as one token and fallback final replies do not duplicate partial provider text.

- [x] **Step 5: Run conversation tests and verify GREEN**

Expected: streaming and non-streaming conversation tests pass.

### Task 4: HTTP SSE Integration

**Files:**
- Modify: `src/project_q/server.py`
- Modify: `tests/test_project_q.py`
- Modify: `HANDOFF.md`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`

- [x] **Step 1: Add failing HTTP SSE native-delta test**

Start the test server with a fake provider stream producing `"Hel"`, `"lo"`, and the rest of a valid plan. Assert:

- SSE token events are `"Hel"` then `"lo"`, not word-split output.
- No `tool_calls`, `memory_writes`, API keys, or raw provider event JSON appears.
- The final event has `done: true`, canonical reply, reasoning mode, and model name.

- [x] **Step 2: Run HTTP test and verify RED**

Expected: current handler emits words after the complete response rather than provider deltas.

- [x] **Step 3: Replace simulated streaming**

Remove the artificial sleep and iterate `self.app.conversations.respond_stream(payload)`. Add:

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
Connection: keep-alive
```

Close the generator on client disconnect and emit one structured SSE error event for post-header failures.

- [x] **Step 4: Update documentation**

Remove the simulated-streaming caveat. Document native streaming for all three providers and retain the honest limitation that structured plan execution waits for full validation before side effects.

- [x] **Step 5: Run focused HTTP tests and verify GREEN**

Expected: owner-session enforcement and native provider delta tests pass.

### Task 5: Final Verification

**Files:**
- No production edits expected.

- [x] **Step 1: Run the full unit suite**

```powershell
$env:PYTHONPATH='src'
& 'C:/Users/umarq.APEX/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m unittest tests.test_project_q
```

Expected: all tests pass.

- [x] **Step 2: Run compile and static checks**

```powershell
& 'C:/Users/umarq.APEX/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -m compileall -q src scripts
& 'C:/Users/umarq.APEX/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe' --check src/project_q/static/app.js
git diff --check
```

Expected: exit code 0 for all checks.

- [x] **Step 3: Run isolated Phase 1 regression verifier**

```powershell
$env:PYTHONPATH='src'
& 'C:/Users/umarq.APEX/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' verify_phase1.py
```

Expected: Phase 1 remains green with 64 tools and 17 Windows tools.

- [x] **Step 4: Run live ephemeral SSE simulation**

Launch an isolated localhost server with a fake provider stream. Read the response incrementally and prove at least two provider-sized token events arrive before the final event, the canonical reply is persisted, and no internal structured plan fields appear in the stream.

- [x] **Step 5: Review the final diff for security**

Check provider credentials, output bounds, parser failure behavior, client disconnect cleanup, audit records, and the invariant that no side effect occurs before complete plan validation.
