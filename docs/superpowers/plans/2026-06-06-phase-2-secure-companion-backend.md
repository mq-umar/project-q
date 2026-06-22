# Phase 2 Secure Companion Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy companion scaffold with a durable, replay-safe, AES-GCM encrypted Windows companion protocol that supports synchronized chat, activity, approvals, routines, memory, quick capture, presence, revocation, and offline delivery.

**Architecture:** Windows remains the sole execution authority. A versioned `CompanionProtocolService` owns Curve25519/HKDF/AES-GCM pairing and envelopes, `CompanionAuthService` owns replay-safe authenticated requests and approval signatures, `SyncEventService` owns append-only device cursors, and `ApprovalService` freezes policy-blocked actions for idempotent owner decisions. `CompanionService` becomes the owner-facing facade used by scoped HTTP routes.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, `cryptography>=45,<47`, `unittest`, existing Project Q HTTP server and DPAPI Vault.

**PRD Coverage:** Phase 2 encrypted relay protocol foundation, session sync, approval flow, remote routine trigger, live task/agent state, quick capture, memory browser, presence, offline delivery, and remote revocation; sections 9.5-9.7, 11.1-11.3, 12.2-12.3, and 13.3.

---

### Task 1: Versioned schema migration and dependency gate

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/project_q/storage.py`
- Test: `tests/test_project_q.py`

- [ ] **Step 1: Write failing schema tests**

Add tests that create a fresh database and assert the following tables and
columns exist:

```python
required_tables = {
    "schema_migrations",
    "companion_pairing_sessions",
    "companion_request_nonces",
    "companion_sync_events",
    "companion_device_cursors",
    "action_requests",
    "action_decisions",
}
```

Assert `companion_devices` includes protocol version, Curve25519 public key,
approval public key, receive sequence, send sequence, presence state, presence
expiry, and `legacy_repair_required`. Assert the migration version is recorded
exactly once after two `Database` initializations.

- [ ] **Step 2: Run the schema tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_project_q.ProjectQApplicationTests.test_phase2_schema_is_versioned_and_idempotent
```

Expected: FAIL because the Phase 2 tables do not exist.

- [ ] **Step 3: Add the cryptography dependency**

Add this runtime dependency:

```toml
"cryptography>=45.0.0,<47.0.0",
```

Do not implement a custom cryptographic fallback. Application startup must
raise a clear dependency error when Phase 2 crypto is imported without the
package.

- [ ] **Step 4: Implement transactional schema migrations**

Refactor `Database._ensure_schema()` so the current base schema remains
idempotent and numbered migrations run inside `BEGIN IMMEDIATE`. Add the Phase
2 tables, indexes, uniqueness constraints, foreign keys where compatible, and
append-only triggers for `action_decisions` and `companion_sync_events`.

Critical constraints:

```sql
UNIQUE(device_id, request_nonce)
UNIQUE(device_id, sequence_number)
UNIQUE(action_request_id)
UNIQUE(recipient_device_id, sequence_number)
```

Legacy paired devices are marked `legacy_repair_required = 1`; their existing
key reference is cleared by the service migration after startup, not exposed.

- [ ] **Step 5: Run the schema test and full storage tests**

Expected: PASS, including repeated startup against the same database.

### Task 2: Standard companion cryptography

**Files:**
- Create: `src/project_q/services/companion_crypto.py`
- Modify: `src/project_q/models.py`
- Test: `tests/test_project_q.py`

- [ ] **Step 1: Write failing key-agreement and envelope tests**

Add tests proving:

```python
windows = CompanionKeyPair.generate()
phone = CompanionKeyPair.generate()
windows_keys = derive_session_keys(
    private_key=windows.private_key,
    peer_public_key=phone.public_key,
    pairing_token=pairing_token,
    local_device_id="windows",
    remote_device_id="phone",
)
phone_keys = derive_session_keys(
    private_key=phone.private_key,
    peer_public_key=windows.public_key,
    pairing_token=pairing_token,
    local_device_id="phone",
    remote_device_id="windows",
)
self.assertEqual(windows_keys.send_key, phone_keys.receive_key)
self.assertEqual(windows_keys.receive_key, phone_keys.send_key)
```

Test AES-256-GCM round trip and rejection of changed ciphertext, nonce, tag,
message ID, recipient, event kind, and sequence. Assert plaintext and raw key
bytes do not appear in serialized envelopes.

- [ ] **Step 2: Run focused crypto tests and verify RED**

Expected: import failure for `project_q.services.companion_crypto`.

- [ ] **Step 3: Implement Curve25519, HKDF, and AES-GCM**

Define:

```python
PROTOCOL_VERSION = 2

@dataclass(frozen=True)
class CompanionSessionKeys:
    send_key: bytes
    receive_key: bytes
    request_key: bytes

class CompanionCryptoService:
    def generate_key_pair(self) -> CompanionKeyPair: ...
    def derive_session_keys(...) -> CompanionSessionKeys: ...
    def seal(self, *, key: bytes, sender_device_id: str,
             recipient_device_id: str, event_kind: str,
             sequence_number: int, payload: dict[str, Any]) -> dict[str, Any]: ...
    def open(self, *, key: bytes, envelope: dict[str, Any],
             expected_sender: str, expected_recipient: str) -> dict[str, Any]: ...
```

Use X25519, HKDF-SHA256, AESGCM, 32-byte keys, 12-byte random nonces, canonical
JSON, and canonical associated data. Do not expose private key serialization
outside the service.

- [ ] **Step 4: Add strict Pydantic request/envelope models**

Add pairing begin/complete, encrypted envelope, sync pull, presence, approval
decision, routine trigger, companion chat, and companion memory models. Bound
all strings, item counts, and cursor/sequence ranges.

- [ ] **Step 5: Run focused crypto/model tests and verify GREEN**

Expected: all crypto tests pass, including 100 random envelope round trips.

### Task 3: One-time pairing and DPAPI key storage

**Files:**
- Create: `src/project_q/services/companion_protocol.py`
- Modify: `src/project_q/services/companion.py`
- Modify: `src/project_q/app.py`
- Test: `tests/test_project_q.py`

- [ ] **Step 1: Write failing pairing lifecycle tests**

Test that pairing start returns:

```python
{
    "protocol_version": 2,
    "device_id": ...,
    "windows_device_id": ...,
    "windows_key_agreement_public_key": ...,
    "pairing_token": ...,
    "expires_at": ...,
}
```

Pairing completion must require the token, phone key-agreement public key, and
P-256 approval public key. Test wrong token, expired token, token reuse,
malformed public keys, and second completion all fail. Assert no shared secret
is returned and only DPAPI-encrypted key references remain after completion.

- [ ] **Step 2: Run pairing tests and verify RED**

Expected: the existing service returns a plaintext `shared_key`.

- [ ] **Step 3: Implement pairing sessions**

`CompanionProtocolService.start_pairing()` creates a ten-minute token and
Windows ephemeral X25519 key. Store hashes and DPAPI-protected private material.
`complete_pairing()` verifies the one-time token, derives session keys, stores
each key under a distinct Vault reference, erases pairing secrets, persists
public keys, and marks the device paired under protocol v2.

- [ ] **Step 4: Implement legacy invalidation**

On startup, find v1 paired devices, delete their old Vault key, clear the
reference, set status `repair_required`, and audit the migration without
logging key material.

- [ ] **Step 5: Run pairing, Vault, migration, and plaintext scans**

Search the database bytes and audit rows for pairing token, ECDH secret,
derived keys, and test payload plaintext. Expected: none are present outside
the encrypted Vault blob.

### Task 4: Replay-safe companion authentication and presence

**Files:**
- Create: `src/project_q/services/companion_auth.py`
- Modify: `src/project_q/services/companion.py`
- Test: `tests/test_project_q.py`

- [ ] **Step 1: Write failing request-auth tests**

Cover a valid request plus rejection for:

- timestamp older than five minutes
- reused request nonce
- sequence equal to or below the last accepted sequence
- changed method, path, body, or device
- revoked and repair-required devices
- invalid MAC

Restart the application against the same database and prove replay state
survives.

- [ ] **Step 2: Run auth tests and verify RED**

Expected: current HMAC verification has no nonce or durable sequence.

- [ ] **Step 3: Implement canonical request authentication**

Authenticate this canonical byte sequence:

```text
PROJECTQ-COMPANION-REQUEST-V2
<device_id>
<method>
<path>
<timestamp>
<request_nonce>
<sequence_number>
<sha256(body)>
```

Verify MAC with the derived request key. Insert the nonce and advance the
sequence in the same transaction so concurrent duplicate requests cannot both
pass.

- [ ] **Step 4: Implement presence**

Allow an authenticated device to publish `active`, `idle`, or `background`
with a maximum 120-second expiry. Presence expires automatically in reads and
is advisory only.

- [ ] **Step 5: Run auth concurrency and restart tests**

Use two threads with the same request. Exactly one must succeed.

### Task 5: Append-only synchronization

**Files:**
- Create: `src/project_q/services/sync.py`
- Modify: `src/project_q/app.py`
- Modify: mutation services under `src/project_q/services/`
- Test: `tests/test_project_q.py`

- [ ] **Step 1: Write failing event ordering tests**

Create conversation, task, agent, memory, routine, audit, approval, and control
changes. Assert each produces an event with increasing global sequence,
resource type/id, operation, redacted payload, and timestamp.

Test:

- bounded initial snapshot
- pull after cursor
- duplicate pull is idempotent
- per-device acknowledgement
- cursor persists after restart
- cursor gap returns `snapshot_required`
- revoked device cannot pull

- [ ] **Step 2: Run sync tests and verify RED**

Expected: no sync service or event tables.

- [ ] **Step 3: Implement `SyncEventService`**

Expose:

```python
append(resource_type, resource_id, operation, payload) -> dict
snapshot(device_id, limits) -> dict
pull(device_id, after_sequence, limit=200) -> dict
acknowledge(device_id, sequence) -> dict
compact(retain_after, active_device_grace_days=30) -> dict
```

Payload builders must exclude secret values, raw provider keys, pairing
material, and unredacted Tier 2/3 parameters.

- [ ] **Step 4: Connect mutation services**

Inject the service into conversations, tasks, agents, memory, routines,
control, approvals, and audit-visible status changes. Append after durable
mutation and use the resource record as the event payload.

- [ ] **Step 5: Run ordering, compaction, and leak tests**

Expected: ordered replay after restart and no plaintext test secret in event
storage.

### Task 6: Durable frozen-action approval queue

**Files:**
- Create: `src/project_q/services/approvals.py`
- Modify: `src/project_q/services/plan_executor.py`
- Modify: `src/project_q/services/policy.py`
- Modify: `src/project_q/services/control.py`
- Modify: `src/project_q/app.py`
- Test: `tests/test_project_q.py`

- [ ] **Step 1: Write failing approval lifecycle tests**

When a Tier 2/3 tool is blocked solely for missing owner approval, assert a
pending action request is created with redacted preview and SHA-256 payload
digest. Test list, approve, reject, expire, execute, fail, and restart.

Security assertions:

- changed payload after approval is rejected
- approval executes at most once under concurrent calls
- Tier 3 requires `biometric_backed=True`
- Tier 2 follows the configured paired-device approval policy
- kill switch prevents approval execution
- external content cannot create an owner decision
- rejected/expired decisions are immutable

- [ ] **Step 2: Run approval tests and verify RED**

Expected: blocked tools are returned only in the chat response.

- [ ] **Step 3: Implement `ApprovalService`**

Define `create_request`, `list_pending`, `get`, `decide`, and
`execute_approved`. Store canonical frozen payload JSON, digest, redacted
preview, goal, session, provenance, model, expiry, and execution outcome.

The service validates the phone P-256 signature over:

```text
PROJECTQ-APPROVAL-V2
<action_id>
<decision>
<tier>
<payload_digest>
<expires_at>
<device_id>
<timestamp>
```

- [ ] **Step 4: Integrate with plan execution**

Extend execution output with `created_action_request_ids`. Only create requests
for approval-only blocks; other policy denials remain blocked without a
resumable action.

- [ ] **Step 5: Run approval and existing policy tests**

Expected: old explicit owner approvals still work, new companion approvals are
durable and idempotent, and no action executes twice.

### Task 7: Scoped companion command API

**Files:**
- Modify: `src/project_q/server.py`
- Modify: `src/project_q/models.py`
- Modify: `src/project_q/services/companion.py`
- Test: `tests/test_project_q.py`

- [ ] **Step 1: Write failing HTTP simulations**

Add signed-device tests for:

```text
POST /api/companion/v2/pairing/complete
GET  /api/companion/v2/sync/snapshot
GET  /api/companion/v2/sync/events
POST /api/companion/v2/sync/ack
POST /api/companion/v2/chat
GET  /api/companion/v2/approvals
POST /api/companion/v2/approvals/{id}/decision
GET  /api/companion/v2/routines
POST /api/companion/v2/routines/{id}/run
GET  /api/companion/v2/memories
POST /api/companion/v2/memories
PUT  /api/companion/v2/memories/{id}
DELETE /api/companion/v2/memories/{id}
POST /api/companion/v2/quick-capture
POST /api/companion/v2/presence
POST /api/companion/v2/control/kill-switch
```

Assert paired-device authentication on every route except pairing completion.
Assert dashboard owner cookies cannot substitute for device auth and companion
auth cannot access settings, Vault, arbitrary tools, or filesystem routes.

- [ ] **Step 2: Run HTTP tests and verify RED**

Expected: the v2 endpoints return 404.

- [ ] **Step 3: Implement one authentication wrapper**

Parse the request body once, verify the canonical raw bytes and headers, then
dispatch a scoped device context. Do not reserialize before MAC verification.

- [ ] **Step 4: Implement bounded handlers**

Chat uses the normal conversation/reasoner/provider path and tags the channel
as `iphone`. Routine runs accept only existing routines and normal policy
checks. Memory operations preserve source/trust metadata. Approval decisions
use the dedicated signature. Responses never include Vault names or secrets.

- [ ] **Step 5: Run all companion HTTP and owner-session tests**

Expected: no authentication regression on dashboard routes.

### Task 8: Phase 2 backend verifier and documentation

**Files:**
- Create: `verify_phase2_backend.py`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/PRD_COMPLETION_MATRIX_2026-06-06.md`
- Test: `tests/test_project_q.py`

- [ ] **Step 1: Build an isolated behavioral verifier**

The verifier creates a temporary Windows runtime, simulates a phone key pair,
pairs, syncs initial state, sends chat through a fake local provider, queues
and decides an approval, triggers a routine, edits memory, sends quick capture,
publishes presence, retries duplicate messages, restarts, and revokes.

- [ ] **Step 2: Add adversarial simulations**

Tamper ciphertext/AAD/tag, replay request/approval, race duplicate approvals,
use stale timestamps, use a revoked device, inject external instructions, scan
database files for known plaintext secrets, and activate the kill switch.

- [ ] **Step 3: Update documentation**

Document protocol v2, re-pairing, device auth headers, approval semantics,
direct/relay transport equivalence, Docker relay prerequisite, and the honest
Mac/Xcode validation gate.

- [ ] **Step 4: Run complete verification**

```powershell
$env:PYTHONPATH='src'
python -m unittest tests.test_project_q
python verify_phase1.py
python verify_phase2_backend.py
python -m compileall -q src scripts verify_phase1.py verify_phase2_backend.py
node --check src/project_q/static/app.js
node --check src/project_q/workers/browser_worker.js
git diff --check
```

Expected: every command exits zero.

- [ ] **Step 5: Record evidence without overclaiming**

Mark the Phase 2 backend/protocol rows complete only after the verifier passes.
Keep native iOS build, APNs, Siri, widget, and physical-device rows open until
the iOS plan and Mac gate are complete.
