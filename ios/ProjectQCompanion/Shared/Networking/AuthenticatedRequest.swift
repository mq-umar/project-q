import CryptoKit
import Foundation

actor RequestSequenceStore {
    private let keychain: KeychainStore
    private let account: String
    private var value: Int64

    init(
        deviceID: String,
        namespace: String = "request-sequence",
        keychain: KeychainStore = KeychainStore()
    ) {
        self.keychain = keychain
        self.account = "\(namespace)-\(deviceID)"
        if let data = try? keychain.data(for: account),
           let text = data.flatMap({ String(data: $0, encoding: .utf8) }),
           let stored = Int64(text) {
            self.value = stored
        } else {
            self.value = 0
        }
    }

    func next() throws -> Int64 {
        value += 1
        try keychain.set(Data(String(value).utf8), for: account)
        return value
    }

    func accept(_ candidate: Int64) throws -> Bool {
        guard candidate > value else {
            return false
        }
        value = candidate
        try keychain.set(Data(String(value).utf8), for: account)
        return true
    }
}

struct AuthenticatedRequestSigner: Sendable {
    let deviceID: String
    let requestKey: Data
    let sequenceStore: RequestSequenceStore

    func sign(
        method: String,
        requestTarget: String,
        body: Data
    ) async throws -> [String: String] {
        guard requestKey.count == 32 else {
            throw CompanionCryptoError.invalidKey
        }
        let timestamp = ISO8601DateFormatter().string(from: Date())
        let nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let sequence = try await sequenceStore.next()
        let bodyHash = SHA256.hash(data: body).map {
            String(format: "%02x", $0)
        }.joined()
        let canonical = Data(
            """
            PROJECTQ-COMPANION-REQUEST-V2
            \(deviceID)
            \(method.uppercased())
            \(requestTarget)
            \(timestamp)
            \(nonce)
            \(sequence)
            \(bodyHash)
            """.utf8
        )
        let signature = HMAC<SHA256>.authenticationCode(
            for: canonical,
            using: SymmetricKey(data: requestKey)
        )
        return [
            "X-Project-Q-Device-ID": deviceID,
            "X-Project-Q-Timestamp": timestamp,
            "X-Project-Q-Request-Nonce": nonce,
            "X-Project-Q-Sequence": String(sequence),
            "X-Project-Q-Signature": Data(signature).base64URLEncodedString(),
        ]
    }
}
