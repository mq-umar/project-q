import CryptoKit
import Foundation

enum CompanionCryptoError: Error {
    case invalidEncoding
    case invalidKey
    case invalidEnvelope
    case unexpectedPeer
}

struct CompanionSessionKeys: Sendable {
    let sendKey: Data
    let receiveKey: Data
    let requestKey: Data
}

enum CompanionCrypto {
    static let algorithm = "X25519-HKDF-SHA256-AES-256-GCM"

    static func generatePrivateKey() -> Curve25519.KeyAgreement.PrivateKey {
        Curve25519.KeyAgreement.PrivateKey()
    }

    static func deriveSessionKeys(
        privateKey: Curve25519.KeyAgreement.PrivateKey,
        peerPublicKey: String,
        pairingToken: Data,
        localDeviceID: String,
        remoteDeviceID: String
    ) throws -> CompanionSessionKeys {
        guard pairingToken.count >= 32,
              let peerData = Data(base64URLEncoded: peerPublicKey),
              peerData.count == 32,
              localDeviceID != remoteDeviceID else {
            throw CompanionCryptoError.invalidKey
        }
        let peer = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: peerData)
        let secret = try privateKey.sharedSecretFromKeyAgreement(with: peer)
        var saltInput = Data("PROJECTQ-COMPANION-PAIRING-V2".utf8)
        saltInput.append(0)
        saltInput.append(pairingToken)
        let salt = Data(SHA256.hash(data: saltInput))
        let ordered = [localDeviceID, remoteDeviceID].sorted()
        let info = Data(
            "PROJECTQ-COMPANION-SESSION-V2\n\(ordered[0])\n\(ordered[1])\n".utf8
        )
        let material = secret.hkdfDerivedSymmetricKey(
            using: SHA256.self,
            salt: salt,
            sharedInfo: info,
            outputByteCount: 96
        )
        let bytes = material.withUnsafeBytes { Data($0) }
        let firstToSecond = bytes.subdata(in: 0..<32)
        let secondToFirst = bytes.subdata(in: 32..<64)
        let requestKey = bytes.subdata(in: 64..<96)
        return CompanionSessionKeys(
            sendKey: localDeviceID == ordered[0] ? firstToSecond : secondToFirst,
            receiveKey: localDeviceID == ordered[0] ? secondToFirst : firstToSecond,
            requestKey: requestKey
        )
    }

    static func seal<Payload: Encodable>(
        _ payload: Payload,
        key: Data,
        senderDeviceID: String,
        recipientDeviceID: String,
        eventKind: String,
        sequenceNumber: Int64,
        createdAt: String = ISO8601DateFormatter().string(from: Date())
    ) throws -> CompanionEnvelope {
        guard key.count == 32, sequenceNumber > 0 else {
            throw CompanionCryptoError.invalidKey
        }
        let metadata: [String: Any] = [
            "created_at": createdAt,
            "event_kind": eventKind,
            "message_id": "msg_\(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased())",
            "protocol_version": 2,
            "recipient_device_id": recipientDeviceID,
            "sender_device_id": senderDeviceID,
            "sequence_number": sequenceNumber,
        ]
        guard let messageID = metadata["message_id"] as? String else {
            throw CompanionCryptoError.invalidEnvelope
        }
        let aad = try JSONSerialization.data(
            withJSONObject: metadata,
            options: [.sortedKeys, .withoutEscapingSlashes]
        )
        let encoder = JSONEncoder.projectQ
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let plaintext = try encoder.encode(payload)
        let sealed = try AES.GCM.seal(
            plaintext,
            using: SymmetricKey(data: key),
            authenticating: aad
        )
        return CompanionEnvelope(
            protocolVersion: 2,
            messageID: messageID,
            senderDeviceID: senderDeviceID,
            recipientDeviceID: recipientDeviceID,
            eventKind: eventKind,
            createdAt: createdAt,
            sequenceNumber: sequenceNumber,
            nonce: Data(sealed.nonce).base64URLEncodedString(),
            ciphertext: sealed.ciphertext.base64URLEncodedString(),
            tag: sealed.tag.base64URLEncodedString(),
            contentType: "application/json",
            algorithm: algorithm
        )
    }

    static func open<Payload: Decodable>(
        _ envelope: CompanionEnvelope,
        key: Data,
        expectedSender: String,
        expectedRecipient: String,
        as type: Payload.Type
    ) throws -> Payload {
        guard key.count == 32,
              envelope.protocolVersion == 2,
              envelope.algorithm == algorithm,
              envelope.senderDeviceID == expectedSender,
              envelope.recipientDeviceID == expectedRecipient,
              let nonceData = Data(base64URLEncoded: envelope.nonce),
              let ciphertext = Data(base64URLEncoded: envelope.ciphertext),
              let tag = Data(base64URLEncoded: envelope.tag),
              nonceData.count == 12,
              tag.count == 16 else {
            throw CompanionCryptoError.invalidEnvelope
        }
        let metadata: [String: Any] = [
            "created_at": envelope.createdAt,
            "event_kind": envelope.eventKind,
            "message_id": envelope.messageID,
            "protocol_version": envelope.protocolVersion,
            "recipient_device_id": envelope.recipientDeviceID,
            "sender_device_id": envelope.senderDeviceID,
            "sequence_number": envelope.sequenceNumber,
        ]
        let aad = try JSONSerialization.data(
            withJSONObject: metadata,
            options: [.sortedKeys, .withoutEscapingSlashes]
        )
        let nonce = try AES.GCM.Nonce(data: nonceData)
        let box = try AES.GCM.SealedBox(
            nonce: nonce,
            ciphertext: ciphertext,
            tag: tag
        )
        let plaintext = try AES.GCM.open(
            box,
            using: SymmetricKey(data: key),
            authenticating: aad
        )
        return try JSONDecoder.projectQ.decode(type, from: plaintext)
    }
}

extension JSONEncoder {
    static var projectQ: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }
}

extension JSONDecoder {
    static var projectQ: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}

extension Data {
    init?(base64URLEncoded value: String) {
        var text = value
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        while text.count % 4 != 0 {
            text.append("=")
        }
        self.init(base64Encoded: text)
    }

    func base64URLEncodedString() -> String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
