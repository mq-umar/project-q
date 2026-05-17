import Foundation

struct ProjectQAPI {
    var baseURL = URL(string: "http://127.0.0.1:8787")!

    func fetchStatus() async throws -> QStatus {
        try await get(path: "api/status", as: QStatus.self)
    }

    func fetchTasks() async throws -> [QTask] {
        let response = try await get(path: "api/tasks", as: Envelope<QTask>.self)
        return response.items
    }

    func fetchMemories() async throws -> [QMemory] {
        let response = try await get(path: "api/memories", as: Envelope<QMemory>.self)
        return response.items
    }

    func fetchAgents() async throws -> [QAgent] {
        let response = try await get(path: "api/agents", as: Envelope<QAgent>.self)
        return response.items
    }

    func fetchAudit() async throws -> [QAuditEvent] {
        let response = try await get(path: "api/audit", as: Envelope<QAuditEvent>.self)
        return response.items
    }

    private func get<T: Decodable>(path: String, as type: T.Type) async throws -> T {
        let (data, _) = try await URLSession.shared.data(from: baseURL.appendingPathComponent(path))
        return try JSONDecoder.projectQ.decode(T.self, from: data)
    }
}

private struct Envelope<T: Decodable>: Decodable {
    let items: [T]
}

private extension JSONDecoder {
    static var projectQ: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}
