# Project Q Companion

This folder contains the native SwiftUI iPhone companion described in the
Project Q PRD. `project.yml` defines the application, widget, share extension,
and unit-test targets for XcodeGen.

Implemented source capabilities:

- expiring QR/deep-link pairing with X25519, HKDF-SHA256, and iOS Keychain storage
- replay-safe authenticated direct requests and AES-256-GCM relay envelopes
- direct token-streaming chat with encrypted relay fallback and offline queueing
- push-to-talk speech input, synchronized history, and spoken replies
- biometric P-256 approval signing and sensitive-view authentication
- tasks, agent activity, routines, approvals, memory editing, quick capture, audit review, and emergency stop
- APNs token registration, generic encrypted-relay wakes, App Intents, widget, share extension, and BackgroundTasks
- encrypted local cache shared through the configured app group and Keychain access group

Generate the Xcode project on macOS:

```bash
cd ios/ProjectQCompanion
xcodegen generate
```

Release gates that require macOS, Xcode, Apple signing, and a physical device:

- compile and run all targets with Swift 6 strict concurrency
- execute `ProjectQCompanionTests`
- verify QR camera pairing and Face ID/Touch ID behavior
- configure APNs credentials and verify sandbox/production delivery
- validate widget, share extension, background refresh, and offline recovery on device

Windows verification uses `scripts/audit_phase2_ios.py`, protocol vectors,
backend/relay simulations, and source-contract checks. It is not a substitute
for the Apple release gates above.
