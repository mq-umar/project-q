import CryptoKit
import XCTest
@testable import ProjectQCompanion

final class ProtocolTests: XCTestCase {
    func testPythonKeyDerivationVector() throws {
        let privateData = try XCTUnwrap(
            Data(base64URLEncoded: "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA")
        )
        let privateKey = try Curve25519.KeyAgreement.PrivateKey(
            rawRepresentation: privateData
        )
        let token = try XCTUnwrap(
            Data(base64URLEncoded: "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVpbXF1eX2A")
        )
        let keys = try CompanionCrypto.deriveSessionKeys(
            privateKey: privateKey,
            peerPublicKey: "WGmv9FBUlzLLqu1eXfmzCm2jHLDldCutWtShp2jxpns",
            pairingToken: token,
            localDeviceID: "phone_1",
            remoteDeviceID: "windows_1"
        )

        XCTAssertEqual(
            keys.sendKey.base64URLEncodedString(),
            "ebnbRU4U3-zGcbkPZDBRCXsPvg34TQK42k5P2I1jK2g"
        )
        XCTAssertEqual(
            keys.receiveKey.base64URLEncodedString(),
            "HbmTL2LlEG-uSzW69TMY95ngtDUENPDRi1jPJzBKvm0"
        )
        XCTAssertEqual(
            keys.requestKey.base64URLEncodedString(),
            "Q_LI4og6vtPWP3UKd3m3lSWK2-9WJClyOB3v3AD5_KA"
        )
    }

    func testAESGCMEnvelopeRoundTripAndTamperRejection() throws {
        struct Payload: Codable, Equatable {
            let text: String
        }
        let key = Data(repeating: 7, count: 32)
        let envelope = try CompanionCrypto.seal(
            Payload(text: "private"),
            key: key,
            senderDeviceID: "phone",
            recipientDeviceID: "windows",
            eventKind: "test",
            sequenceNumber: 1
        )
        let opened = try CompanionCrypto.open(
            envelope,
            key: key,
            expectedSender: "phone",
            expectedRecipient: "windows",
            as: Payload.self
        )
        XCTAssertEqual(opened, Payload(text: "private"))

        let tampered = CompanionEnvelope(
            protocolVersion: envelope.protocolVersion,
            messageID: envelope.messageID,
            senderDeviceID: envelope.senderDeviceID,
            recipientDeviceID: envelope.recipientDeviceID,
            eventKind: "changed",
            createdAt: envelope.createdAt,
            sequenceNumber: envelope.sequenceNumber,
            nonce: envelope.nonce,
            ciphertext: envelope.ciphertext,
            tag: envelope.tag,
            contentType: envelope.contentType,
            algorithm: envelope.algorithm
        )
        XCTAssertThrowsError(
            try CompanionCrypto.open(
                tampered,
                key: key,
                expectedSender: "phone",
                expectedRecipient: "windows",
                as: Payload.self
            )
        )
    }
}
