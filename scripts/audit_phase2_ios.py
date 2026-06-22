from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS_ROOT = ROOT / "ios" / "ProjectQCompanion"


def read(relative_path: str) -> str:
    return (IOS_ROOT / relative_path).read_text(encoding="utf-8")


def main() -> int:
    failures: list[str] = []
    checks = 0

    def require(condition: bool, message: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(message)

    required_files = (
        "project.yml",
        "ProjectQCompanion/Features/Pairing/PairingView.swift",
        "ProjectQCompanion/Features/Chat/ChatView.swift",
        "ProjectQCompanion/Features/Approvals/ApprovalsView.swift",
        "ProjectQCompanion/Features/Routines/RoutinesView.swift",
        "ProjectQCompanion/Features/Memory/MemoryBrowserView.swift",
        "ProjectQCompanion/Features/Capture/QuickCaptureView.swift",
        "ProjectQCompanion/Features/Audit/AuditLogView.swift",
        "ProjectQCompanion/Intents/ProjectQShortcuts.swift",
        "ProjectQWidgetExtension/ProjectQWidget.swift",
        "ProjectQShareExtension/ShareViewController.swift",
        "ProjectQCompanionTests/ProtocolTests.swift",
        "ProjectQCompanionTests/Fixtures/protocol-v2.json",
        "Shared/Security/CompanionCrypto.swift",
        "Shared/Security/KeychainStore.swift",
        "Shared/Security/BiometricApprovalSigner.swift",
        "Shared/Security/SensitiveAccessController.swift",
        "Shared/Storage/EncryptedCache.swift",
        "Shared/Networking/AuthenticatedRequest.swift",
        "Shared/Networking/DirectTransport.swift",
        "Shared/Networking/RelayTransport.swift",
        "Shared/Networking/OfflineQueueTransport.swift",
        "Shared/Sync/SyncEngine.swift",
        "Shared/Sync/OfflineQueue.swift",
    )
    for relative_path in required_files:
        require((IOS_ROOT / relative_path).is_file(), f"missing {relative_path}")

    project = read("project.yml")
    for token in (
        "ProjectQCompanion:",
        "ProjectQWidgetExtension:",
        "ProjectQShareExtension:",
        "ProjectQCompanionTests:",
        "NSCameraUsageDescription",
        "NSFaceIDUsageDescription",
        "NSLocalNetworkUsageDescription",
        "NSAllowsLocalNetworking",
        "ProjectQKeychainAccessGroup",
        "BGTaskSchedulerPermittedIdentifiers",
    ):
        require(token in project, f"project.yml missing {token}")

    combined_swift = "\n".join(
        path.read_text(encoding="utf-8")
        for path in IOS_ROOT.rglob("*.swift")
    )
    for token in (
        "Curve25519.KeyAgreement",
        "AES.GCM",
        "SecureEnclave.P256.Signing.PrivateKey",
        "LocalAuthentication",
        "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly",
        "SFSpeechRecognizer",
        "AVAudioEngine",
        "BGTaskScheduler",
        "UNUserNotificationCenter",
        "AppShortcutsProvider",
        "WidgetKit",
        "SLComposeServiceViewController",
        "DataScannerViewController",
        "registerForRemoteNotifications",
        "registerAPNs",
        "ProjectQAPNsEnvironment",
        "commandID",
        "companion_response",
        "AsyncThrowingStream<CompanionChatStreamEvent",
        "/api/companion/v2/chat/stream",
        "streamChat",
        "CompanionAuditEntry",
        "AuditLogView",
        "lastRunOutcome",
        "Completed",
        "captureType",
        "speech.transcript",
        "deviceOwnerAuthenticationWithBiometrics",
        "SensitiveAccessController",
    ):
        require(token in combined_swift, f"iOS source missing {token}")

    for forbidden in (
        "PROJECTQ-HMAC-SHA256-STREAM-V1",
        "xorStream",
        "sharedKey",
        "allowsArbitraryLoads",
        " as!",
        "try!",
        "fatalError(",
    ):
        require(forbidden not in combined_swift, f"forbidden iOS token: {forbidden}")

    keychain = read("Shared/Security/KeychainStore.swift")
    require(
        "configuredAccessGroup" in keychain
        and "ProjectQKeychainAccessGroup" in keychain,
        "shared Keychain access group is not selected at runtime",
    )

    quick_capture = read(
        "ProjectQCompanion/Features/Capture/QuickCaptureView.swift"
    )
    for token in (
        'tag("text")',
        'tag("voice")',
        'tag("url")',
        "SpeechController",
        "speech.transcript",
        "type: captureType",
    ):
        require(token in quick_capture, f"quick capture missing {token}")

    approvals = read(
        "ProjectQCompanion/Features/Approvals/ApprovalsView.swift"
    )
    require(
        'Section("Completed")' in approvals
        and "executionOutcome" in approvals,
        "approval history and execution outcome are not visible",
    )

    speech = read(
        "ProjectQCompanion/Features/Chat/SpeechController.swift"
    )
    require(
        "tapInstalled" in speech
        and "if tapInstalled" in speech,
        "speech audio tap lifecycle is not guarded",
    )

    for relative_path in (
        "ProjectQCompanion/Features/Memory/MemoryBrowserView.swift",
        "ProjectQCompanion/Features/Audit/AuditLogView.swift",
    ):
        sensitive_view = read(relative_path)
        require(
            "SensitiveAccessController" in sensitive_view
            and "isUnlocked" in sensitive_view,
            f"{relative_path} is not biometric-gated",
        )

    fixture = json.loads(
        read("ProjectQCompanionTests/Fixtures/protocol-v2.json")
    )
    require(fixture.get("protocol_version") == 2, "protocol fixture is not v2")
    require(
        len(str(fixture.get("pairing_token", ""))) >= 40,
        "protocol fixture pairing token is too short",
    )

    if failures:
        print("Phase 2 iOS static audit failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Phase 2 iOS static audit passed ({checks} checks).")
    print("Mac/Xcode build, signing, APNs, simulator, and device gates remain required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
