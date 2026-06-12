import Foundation

actor OfflineQueue {
    private let cache: EncryptedCache
    private let cacheName = "offline-command-queue"
    private var commands: [OfflineCommand]

    init(cache: EncryptedCache = EncryptedCache()) {
        self.cache = cache
        self.commands = []
    }

    func load() async {
        commands = (try? await cache.read([OfflineCommand].self, named: cacheName)) ?? []
    }

    func enqueue(
        commandID: UUID = UUID(),
        method: String,
        path: String,
        body: Data?
    ) async {
        guard !commands.contains(where: { $0.id == commandID }) else {
            return
        }
        commands.append(
            OfflineCommand(
                id: commandID,
                method: method,
                path: path,
                body: body,
                createdAt: Date(),
                attemptCount: 0
            )
        )
        try? await persist()
    }

    func pendingCount() -> Int {
        commands.count
    }

    func replace(with commands: [OfflineCommand]) async {
        self.commands = commands
        try? await persist()
    }

    func allCommands() -> [OfflineCommand] {
        commands
    }

    private func persist() async throws {
        try await cache.write(commands, named: cacheName)
    }
}
