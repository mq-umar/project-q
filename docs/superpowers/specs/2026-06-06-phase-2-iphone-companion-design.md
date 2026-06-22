# Project Q Phase 2 iPhone Companion Design

## Status

Approved architecture direction: native SwiftUI companion with an
end-to-end-encrypted relay and Windows as the authoritative execution runtime.

This specification covers PRD roadmap Phase 2:

- iPhone voice/text chat and session sync
- push notifications and approval flows
- remote Windows routine triggers
- App Shortcuts and Siri integration
- end-to-end encrypted relay
- live task and agent status
- quick capture and memory browsing

It also covers the supporting iPhone requirements in PRD sections 11 and 13.3:
biometric protection, widgets, share extension, background sync, presence,
offline delivery, ciphertext-only relay storage, and remote revocation.

## Constraints

- Development is currently running on Windows. The repository must contain a
  complete native iOS source project and deterministic XcodeGen project
  definition, but final compilation, signing, APNs entitlement validation, and
  physical-device testing require macOS with Xcode.
- Windows remains the only authority allowed to execute tools, agents, and
  routines. The iPhone is a secure owner input, approval, and status surface.
- Relay compromise must not expose message content or let the relay authorize
  actions.
- Existing Phase 1 local dashboard and provider support must remain functional.
- Ollama, OpenAI Responses-compatible APIs, and Anthropic Messages continue to
  use the same Windows-side conversation and policy path.

## Architecture

The system has three independently testable components:

1. **Windows runtime**
   - Owns conversations, memories, tasks, agents, routines, approvals, audit,
     model routing, and all tool execution.
   - Exposes companion-authenticated commands and an append-only sync event
     stream.
   - Encrypts and signs every relay payload before upload.

2. **Native iPhone companion**
   - SwiftUI application with Chat, Activity, Approvals, Memory, Capture, and
     Settings surfaces.
   - Uses Keychain, LocalAuthentication, CryptoKit, Speech, AVFoundation,
     BackgroundTasks, UserNotifications, App Intents, WidgetKit, and a share
     extension.
   - Maintains an encrypted local cache and an offline outbound queue.

3. **Minimal relay**
   - A separately runnable FastAPI service with WebSocket and polling APIs.
   - Stores opaque encrypted envelopes keyed by recipient device and message
     identifier.
   - Tracks short-lived presence, delivery acknowledgements, APNs routing
     metadata, and revocations.
   - Never receives content keys or decryptable payloads.

The iPhone prefers a direct local connection to Windows when reachable. It
uses the same encrypted envelope protocol over the relay when direct
connectivity is unavailable. Transport choice cannot change authorization or
message semantics.

## Cryptographic Protocol

The current `PROJECTQ-HMAC-SHA256-STREAM-V1` XOR stream construction is removed.
Phase 2 uses versioned, standard primitives implemented by Python
`cryptography` and Apple CryptoKit.

### Device identity and pairing

Each device owns:

- a Curve25519 key-agreement private key
- a P-256 approval-signing key
- a random device identifier

The iPhone stores private keys in Keychain with
`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`. The approval key is created
with Secure Enclave protection when supported and requires
`biometryCurrentSet` for signatures. Windows stores its private material
through the existing DPAPI Vault.

Pairing is local and one-time:

1. Windows creates a pending device record, a 256-bit one-time token, expiry,
   and its public key bundle.
2. The dashboard displays a QR payload containing the Windows device ID,
   public key, local endpoint, optional relay endpoint, token, protocol version,
   and expiry.
3. The iPhone generates its key bundle and sends a pairing completion message
   containing its public keys and proof of possession.
4. Both sides derive the same master secret using Curve25519 ECDH and
   HKDF-SHA256.
5. HKDF domain separation derives independent keys for Windows-to-iPhone
   encryption, iPhone-to-Windows encryption, request authentication, and local
   cache wrapping.
6. The one-time token is erased. No shared symmetric key is returned by an API.

Pairing tokens expire after ten minutes and can be used only once.

### Envelope format

Every synchronized payload uses AES-256-GCM with a fresh 96-bit nonce.
Authenticated associated data contains:

- protocol version
- message ID
- sender device ID
- recipient device ID
- event kind
- creation timestamp
- sequence number

The encrypted payload is canonical JSON. Envelope records include ciphertext,
nonce, GCM tag, associated-data fields, and a sender authentication code.
Decryption rejects:

- altered ciphertext, tags, or associated data
- an unexpected sender or recipient
- reused message IDs or nonces
- stale command timestamps
- revoked devices
- non-monotonic command sequence numbers

Protocol test vectors are generated in Python and checked by Swift tests on a
Mac. Windows tests independently validate tamper, replay, expiry, and
cross-device direction separation.

## Companion Authentication and Authorization

General companion commands require:

- paired, non-revoked device
- current timestamp within five minutes
- unique request nonce
- monotonic per-device sequence
- authentication using the derived request key

Tier 2 and Tier 3 decisions use a separate approval signature. The iOS app asks
LocalAuthentication to unlock the approval private key before signing the
exact approval record. The signed record contains action ID, decision, tier,
payload digest, expiry, device ID, and timestamp.

Tier 3 always requires a fresh biometric-backed signature. Tier 2 follows the
owner profile: biometric approval by default, with an explicit setting for
trusted paired-device approval. Relay delivery is never evidence of approval.

Revocation immediately:

- marks the device revoked
- deletes its Windows-derived keys from DPAPI
- rejects future requests and envelopes
- invalidates pending approvals from the device
- publishes a relay revocation marker
- terminates active companion sessions

## Durable Approval Queue

The Windows database gains `action_requests` and `action_decisions`.

An action request stores:

- action ID and originating session
- action tier and tool name
- owner-readable summary
- redacted parameter preview and payload digest
- originating goal and trust-zone provenance
- model and plan identifiers
- status: `pending`, `approved`, `rejected`, `expired`, `executed`, or `failed`
- creation and expiry timestamps

When policy blocks a Tier 2 or Tier 3 action only because owner approval is
missing, the executor creates an approval request instead of losing the action.
The exact validated action is frozen by digest. Approval resumes only that
frozen action; it never causes the model to regenerate a broader command.

Decision records are append-only. Approval execution is idempotent, and
double-tap or redelivery cannot run the action twice.

## Sync and Presence

Windows writes owner-visible changes to an append-only `sync_events` table.
Events include conversations, tasks, agents, approvals, memories, routines,
audit summaries, control status, notifications, and device presence changes.

Each device maintains a cursor. Sync supports:

- initial bounded snapshot
- ordered incremental events
- acknowledgement and cursor advancement
- idempotent redelivery
- retention-based compaction after every active device has acknowledged
- polling fallback when WebSocket is unavailable

Presence is advisory only. Devices publish an active/idle/background state with
a short expiry. Presence affects notification routing and UI hints but never
authorization.

## iPhone Application

The repository gains an XcodeGen `project.yml` with these targets:

- `ProjectQCompanion` application
- `ProjectQWidgetExtension`
- `ProjectQShareExtension`
- `ProjectQCompanionTests`

### Chat

- Full synchronized conversation history.
- Text composer with streamed assistant response.
- Push-to-talk using `AVAudioEngine` and `SFSpeechRecognizer`.
- Interim transcription remains editable before submission.
- Response text is spoken in sentence-sized chunks through
  `AVSpeechSynthesizer`; tapping the microphone interrupts speech.
- Text-only mode remains fully functional.

### Activity dashboard

- Active task queue with status and priority.
- Agent status, progress, current stage, elapsed time, and failure state.
- Windows online/offline and direct/relay transport indicators.
- Pull-to-refresh plus encrypted live event updates.

### Approvals

- Pending requests ordered by urgency and expiry.
- Redacted parameter preview, reason, tier, originating goal, model, and source
  trust zones.
- Approve or reject without leaving the app.
- Biometric prompt before approval signatures.
- Completed decision history and execution outcome.

### Routines

- List trusted routines and recent runs.
- One-tap remote trigger.
- Tier ceiling and required approval shown before execution.
- Run status and step outcomes synchronized back to the phone.

### Quick capture and memory

- Text, dictated note, URL, and shared content capture.
- Offline queue with visible delivery state.
- Memory search, filtering, detail, edit, confirmation, and deletion.
- External/shared content is origin-tagged and cannot directly authorize tools.

### Platform integration

- App Intents for Ask Q, Quick Capture, Run Routine, and Emergency Stop.
- App Shortcuts phrases and Spotlight exposure.
- Home/lock-screen widget with status, open tasks, and quick capture.
- Share extension for URL, text, and files.
- Background refresh for queued delivery and status sync.
- Rich local/push notifications with approval deep links.

## Relay Service

The relay API provides:

- device registration using opaque routing identifiers
- authenticated WebSocket connections
- encrypted envelope upload
- recipient inbox polling fallback
- delivery acknowledgement
- short-lived presence
- revocation propagation
- APNs token routing and notification hook
- health and metrics endpoints with no message content

Relay storage contains only envelope metadata necessary for routing and opaque
ciphertext. Messages are deleted after acknowledgement or expiry. Logs exclude
ciphertext, tokens, APNs device tokens, IP-derived identity, and all payload
content.

APNs sending is adapter-based. Tests use a recording adapter. Production APNs
requires owner-supplied Apple credentials and is an environment verification
gate, not a hardcoded secret.

The relay ships with Docker configuration, SQLite for single-owner deployment,
and documented environment variables so it can run locally, on an owner VPS,
or on a small managed host.

## Windows Companion API

Companion-only endpoints use device authentication and return only scoped
owner data:

- pairing start/complete/status
- encrypted sync snapshot and event pull
- chat submit and stream cursor
- task and agent status
- pending approval list and signed decision
- routine list and trigger
- memory search, create, update, and delete
- quick capture
- device presence
- relay configuration and revocation
- emergency stop

The existing unauthenticated iOS reads are removed. Dashboard owner-session
routes remain separate.

## Failure Handling

- Direct connection failure automatically selects relay transport.
- Relay failure keeps outbound items in the local encrypted queue.
- Windows offline state is explicit; commands remain queued until expiry.
- Key mismatch or authentication failure never falls back to plaintext.
- Cursor gaps trigger a fresh encrypted snapshot.
- Expired approvals become non-executable.
- Server restart preserves queued actions, sync events, device sequences, and
  decisions.
- Kill switch blocks chat actions, routine triggers, approvals, and new agent
  work while still permitting status, audit, and explicit emergency recovery.

## Migration

Existing companion records using `PROJECTQ-HMAC-SHA256-STREAM-V1` are marked
`legacy_repair_required`. Their shared secrets are deleted from the Vault and
they must pair again. Legacy envelopes remain unreadable metadata for audit or
are deleted through the owner data-control flow; they are never silently
upgraded.

Schema migrations are additive and versioned. Startup migration is
transactional and can safely retry.

## Verification

### Windows automated tests

- pairing proof and one-time expiry
- ECDH/HKDF key agreement
- AES-GCM round trip and direction separation
- ciphertext, tag, associated-data, nonce, and replay tampering
- companion request timestamp, nonce, sequence, and revocation
- approval creation, biometric-signature validation, expiry, rejection,
  frozen-payload digest, idempotency, and single execution
- sync ordering, snapshots, cursor resume, redelivery, compaction, and restart
- relay blindness, acknowledgement, expiry, presence, and revocation
- direct-to-relay failover and offline queue simulation
- chat, routine, memory, quick capture, audit, and emergency HTTP flows
- plaintext-secret and plaintext-message scans over databases and relay logs

### iOS source checks on Windows

- complete target/source manifest
- no placeholder UI text or constant security toggles
- Keychain and LocalAuthentication usage
- AES-GCM and Curve25519 CryptoKit usage
- Speech, AVFoundation, BackgroundTasks, UserNotifications, App Intents,
  WidgetKit, and share-extension wiring
- Python-generated protocol fixtures embedded in Swift tests

### Required Mac/Xcode gate

- generate the project with XcodeGen
- build all targets with `xcodebuild`
- run unit and UI tests on an iPhone simulator
- verify Keychain, biometrics simulation, App Intents, widget, share extension,
  background refresh, notifications, and offline queue
- validate signing, entitlements, APNs, and a physical-device pairing round trip

Windows completion must state this gate honestly until Mac evidence exists.

## Acceptance Criteria

Phase 2 is implementation-complete when:

1. Every Phase 2 roadmap item maps to a behavioral test or the explicit
   Mac/Xcode environment gate.
2. No companion command endpoint accepts an unpaired or revoked device.
3. The relay cannot decrypt payloads or authorize actions.
4. Tier 3 actions cannot execute without a fresh biometric-backed signature.
5. Offline and duplicate delivery cannot lose or double-execute commands.
6. Existing local dashboard and Ollama/OpenAI/Anthropic paths still pass.
7. The full Python suite, relay tests, protocol simulations, static iOS audit,
   and security leak scans pass.
8. Documentation distinguishes source completeness from Mac/device validation.
