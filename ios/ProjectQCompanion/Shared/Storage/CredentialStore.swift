import Foundation

actor CredentialStore {
    private let keychain: KeychainStore
    private let account = "companion-session-v2"

    init(keychain: KeychainStore = KeychainStore()) {
        self.keychain = keychain
    }

    func load() throws -> CompanionSession? {
        guard let data = try keychain.data(for: account) else {
            return nil
        }
        return try JSONDecoder.projectQ.decode(CompanionSession.self, from: data)
    }

    func save(_ session: CompanionSession) throws {
        try keychain.set(
            try JSONEncoder.projectQ.encode(session),
            for: account
        )
    }

    func clear() throws {
        try keychain.delete(account)
    }
}
