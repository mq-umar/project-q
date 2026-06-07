# Phase 2 Native iOS Companion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing SwiftUI skeleton into a complete native Project Q companion source project for chat, voice, approvals, live activity, routines, memory, quick capture, notifications, App Intents, widgets, sharing, background sync, offline delivery, and revocation.

**Architecture:** XcodeGen defines the app, widget, share extension, and tests. A shared Swift package-style source layer owns CryptoKit protocol compatibility, Keychain, authenticated transport, sync cursors, encrypted local cache, and offline queue. Feature views consume one `CompanionStore`; Windows remains the execution authority.

**Tech Stack:** Swift 6, SwiftUI, CryptoKit, Security, LocalAuthentication, Speech, AVFoundation, BackgroundTasks, UserNotifications, AppIntents, WidgetKit, XCTest, XcodeGen.

**PRD Coverage:** All Phase 2 roadmap items and PRD sections 11.1-11.3.

---

### Task 1: Buildable project definition and shared architecture

**Files:**
- Create: `ios/ProjectQCompanion/project.yml`
- Create: `ios/ProjectQCompanion/Config/*.xcconfig`
- Reorganize: `ios/ProjectQCompanion/ProjectQCompanion/`
- Create: app, widget, share-extension, and test target directories

- [ ] Add a Windows static test that validates every source path referenced by `project.yml`.
- [ ] Run it and confirm failure because no project definition exists.
- [ ] Define iOS 17+ app, widget, share extension, unit test, URL scheme, App Groups, Keychain groups, background modes, speech, microphone, biometrics, and notification capabilities.
- [ ] Remove placeholder text and constant security toggles from the existing skeleton.
- [ ] Add a Mac gate command: `xcodegen generate && xcodebuild ... test`.

### Task 2: CryptoKit, Keychain, pairing, and encrypted cache

**Files:**
- Create: `Shared/Security/CompanionCrypto.swift`
- Create: `Shared/Security/KeychainStore.swift`
- Create: `Shared/Security/BiometricApprovalSigner.swift`
- Create: `Shared/Storage/EncryptedCache.swift`
- Create: `ProjectQCompanionTests/ProtocolTests.swift`

- [ ] Import Python-generated protocol vectors and write XCTest cases for X25519/HKDF/AES-GCM compatibility.
- [ ] Implement Secure Enclave P-256 approval keys with biometric access control and software fallback only on unsupported simulators.
- [ ] Store device identity, cursors, and queue-wrapping keys in Keychain; store cache and queue records encrypted with AES-GCM.
- [ ] Implement QR pairing import and proof-of-possession completion.
- [ ] Add static Windows checks for CryptoKit, Keychain access control, LocalAuthentication, and absence of plaintext secret persistence.

### Task 3: Authenticated direct and relay transports

**Files:**
- Create: `Shared/Networking/CompanionTransport.swift`
- Create: `Shared/Networking/DirectTransport.swift`
- Create: `Shared/Networking/RelayTransport.swift`
- Create: `Shared/Networking/AuthenticatedRequest.swift`
- Create: `Shared/Sync/SyncEngine.swift`
- Create: `Shared/Sync/OfflineQueue.swift`

- [ ] Write transport-state and queue XCTest cases with URLProtocol fakes.
- [ ] Implement request nonce, durable sequence, timestamp, and request MAC.
- [ ] Prefer direct Windows transport, fail over to relay, and never fail over to plaintext.
- [ ] Implement WebSocket event delivery with polling fallback, cursor acknowledgement, snapshot recovery, and idempotent offline queue delivery.
- [ ] Surface direct/relay/offline state to the UI.

### Task 4: Chat and push-to-talk

**Files:**
- Create: `Features/Chat/ChatView.swift`
- Create: `Features/Chat/ChatViewModel.swift`
- Create: `Features/Chat/SpeechController.swift`

- [ ] Write view-model tests for synchronized history, streamed deltas, interruption, failed send, and queued send.
- [ ] Implement text chat through companion v2 APIs.
- [ ] Implement editable interim speech recognition and sentence-buffered speech synthesis.
- [ ] Cancel spoken response when microphone capture begins.
- [ ] Keep a complete text-only path when permissions or hardware are unavailable.

### Task 5: Activity, approvals, routines, memory, and capture

**Files:**
- Create feature modules under `Features/Activity`, `Features/Approvals`, `Features/Routines`, `Features/Memory`, and `Features/Capture`
- Modify: `ProjectQCompanion/ContentView.swift`

- [ ] Write store/view-model tests for live task/agent progress, approval expiry, biometric decisions, routine runs, memory search/edit/delete, and queued captures.
- [ ] Implement task/agent dashboard and Windows presence.
- [ ] Implement approval detail with redacted parameters, trust provenance, expiry, biometric approve/reject, and outcome history.
- [ ] Implement trusted-routine list and trigger.
- [ ] Implement memory browser with search, filter, edit, confirmation, and delete.
- [ ] Implement text, URL, and dictated quick capture with visible offline delivery state.

### Task 6: Notifications and background work

**Files:**
- Create: `Shared/Notifications/NotificationManager.swift`
- Create: `Shared/Background/BackgroundSync.swift`
- Modify app lifecycle files

- [ ] Write scheduling and deep-link parsing tests.
- [ ] Register APNs token, send only opaque token metadata to the relay, and handle task, agent, approval, alert, and briefing notification categories.
- [ ] Add approval deep links without embedding sensitive action details in push payloads.
- [ ] Register bounded `BGAppRefreshTask` and `BGProcessingTask` handlers for queue flush and sync.

### Task 7: App Intents, widget, and share extension

**Files:**
- Create: `ProjectQCompanion/Intents/*.swift`
- Create: `ProjectQWidgetExtension/*.swift`
- Create: `ProjectQShareExtension/*.swift`

- [ ] Write App Intent parameter and route tests where Xcode permits.
- [ ] Implement Ask Q, Quick Capture, Run Routine, and Emergency Stop intents and App Shortcuts phrases.
- [ ] Implement a status/open-task widget with quick-capture deep link.
- [ ] Implement share ingestion for text, URLs, and files into the encrypted offline queue.
- [ ] Require authentication before sensitive intent results or approval routes.

### Task 8: Phase 2 iOS audit and Mac gate

**Files:**
- Create: `scripts/audit_phase2_ios.py`
- Modify: `ios/ProjectQCompanion/README.md`
- Modify: `docs/PRD_COMPLETION_MATRIX_2026-06-06.md`

- [ ] Build a Windows static audit covering project targets, required frameworks, entitlements, privacy descriptions, security APIs, feature routes, and placeholder removal.
- [ ] Generate protocol fixtures and verify Swift test references.
- [ ] Run the Windows backend, relay, and iOS static simulations together.
- [ ] On macOS, generate the Xcode project, build every target, run unit/UI tests on an iPhone simulator, and verify biometrics simulation, widget, share extension, App Intents, notifications, background sync, and offline queue.
- [ ] Keep signing, real APNs, Siri-on-device, and physical pairing explicitly open until owner credentials and hardware produce evidence.
