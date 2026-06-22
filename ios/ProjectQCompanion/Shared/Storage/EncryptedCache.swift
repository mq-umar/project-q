import CryptoKit
import Foundation
import Security

actor EncryptedCache {
    static let appGroup = "group.com.projectq.companion"

    private let keychain: KeychainStore
    private let directory: URL
    private let keyAccount = "encrypted-cache-key-v1"

    init(keychain: KeychainStore = KeychainStore()) {
        self.keychain = keychain
        self.directory = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: Self.appGroup
        ) ?? FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        )[0]
    }

    func write<Value: Encodable & Sendable>(
        _ value: Value,
        named name: String
    ) throws {
        let plaintext = try JSONEncoder.projectQ.encode(value)
        let sealed = try AES.GCM.seal(
            plaintext,
            using: SymmetricKey(data: try cacheKey())
        )
        guard let combined = sealed.combined else {
            throw CompanionCryptoError.invalidEnvelope
        }
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        try combined.write(
            to: directory.appendingPathComponent(name).appendingPathExtension("pqcache"),
            options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
        )
    }

    func read<Value: Decodable & Sendable>(
        _ type: Value.Type,
        named name: String
    ) throws -> Value? {
        let url = directory.appendingPathComponent(name).appendingPathExtension("pqcache")
        guard FileManager.default.fileExists(atPath: url.path) else {
            return nil
        }
        let combined = try Data(contentsOf: url)
        let box = try AES.GCM.SealedBox(combined: combined)
        let plaintext = try AES.GCM.open(
            box,
            using: SymmetricKey(data: try cacheKey())
        )
        return try JSONDecoder.projectQ.decode(type, from: plaintext)
    }

    func remove(named name: String) throws {
        let url = directory.appendingPathComponent(name).appendingPathExtension("pqcache")
        try? FileManager.default.removeItem(at: url)
    }

    private func cacheKey() throws -> Data {
        if let existing = try keychain.data(for: keyAccount), existing.count == 32 {
            return existing
        }
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw CompanionCryptoError.invalidKey
        }
        let key = Data(bytes)
        try keychain.set(key, for: keyAccount)
        return key
    }
}
