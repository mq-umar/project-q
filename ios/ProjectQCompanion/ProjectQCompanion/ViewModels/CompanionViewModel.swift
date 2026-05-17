import Foundation

@MainActor
final class CompanionViewModel: ObservableObject {
    @Published var status: QStatus?
    @Published var tasks: [QTask] = []
    @Published var memories: [QMemory] = []
    @Published var agents: [QAgent] = []
    @Published var auditEvents: [QAuditEvent] = []

    private let api = ProjectQAPI()

    func refresh() async {
        do {
            status = try await api.fetchStatus()
            tasks = try await api.fetchTasks()
            agents = try await api.fetchAgents()
            memories = try await api.fetchMemories()
            auditEvents = try await api.fetchAudit()
        } catch {
            print("Project Q companion refresh failed: \(error)")
        }
    }
}

