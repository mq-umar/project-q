import Foundation

actor SyncEngine {
    private let api: CompanionAPI
    private let cache: EncryptedCache
    private var cursor: Int64

    init(
        api: CompanionAPI,
        cache: EncryptedCache = EncryptedCache(),
        cursor: Int64 = 0
    ) {
        self.api = api
        self.cache = cache
        self.cursor = cursor
    }

    func initialSnapshot() async throws -> CompanionSnapshot {
        let snapshot = try await api.snapshot()
        cursor = snapshot.snapshotSequence
        try await cache.write(snapshot, named: "latest-snapshot")
        try await api.acknowledge(cursor)
        return snapshot
    }

    func cachedSnapshot() async -> CompanionSnapshot? {
        try? await cache.read(CompanionSnapshot.self, named: "latest-snapshot")
    }

    func poll() async throws -> [CompanionSyncEvent] {
        let page = try await api.events(after: cursor)
        if page.snapshotRequired {
            _ = try await initialSnapshot()
            return []
        }
        if let last = page.items.last {
            cursor = last.sequenceNumber
            try await api.acknowledge(cursor)
        }
        return page.items
    }
}
