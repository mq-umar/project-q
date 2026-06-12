import Foundation
import SwiftUI

@MainActor
final class CompanionStore: ObservableObject {
    @Published private(set) var tasks: [CompanionTask] = []
    @Published private(set) var agents: [CompanionAgent] = []
    @Published private(set) var memories: [CompanionMemory] = []
    @Published private(set) var routines: [CompanionRoutine] = []
    @Published private(set) var approvals: [CompanionApproval] = []
    @Published private(set) var audit: [CompanionAuditEntry] = []
    @Published private(set) var messages: [CompanionConversationMessage] = []
    @Published private(set) var control = CompanionControlStatus(
        active: false,
        reason: "",
        activatedAt: "",
        source: ""
    )
    @Published private(set) var transportState: CompanionTransportState = .unpaired
    @Published private(set) var pendingOfflineCount = 0
    @Published var lastError: String?
    @Published var isRefreshing = false

    private let credentials: CredentialStore
    private let cache: EncryptedCache
    private let queue: OfflineQueue
    private let approvalSigner: BiometricApprovalSigner
    private let pairingCoordinator: PairingCoordinator
    private var api: CompanionAPI?
    private var syncEngine: SyncEngine?
    private var directTransport: DirectTransport?
    private var relayTransport: RelayTransport?
    private var syncTask: Task<Void, Never>?

    init(
        credentials: CredentialStore = CredentialStore(),
        cache: EncryptedCache = EncryptedCache(),
        queue: OfflineQueue = OfflineQueue(),
        approvalSigner: BiometricApprovalSigner = BiometricApprovalSigner(),
        pairingCoordinator: PairingCoordinator = PairingCoordinator()
    ) {
        self.credentials = credentials
        self.cache = cache
        self.queue = queue
        self.approvalSigner = approvalSigner
        self.pairingCoordinator = pairingCoordinator
    }

    static func live() -> CompanionStore {
        CompanionStore()
    }

    func start() async {
        await queue.load()
        pendingOfflineCount = await queue.pendingCount()
        guard let session = try? await credentials.load() else {
            transportState = .unpaired
            return
        }
        configure(session)
        if let cached = await syncEngine?.cachedSnapshot() {
            apply(cached)
        }
        await refresh()
        startPolling()
    }

    func refresh() async {
        guard let syncEngine else {
            transportState = .unpaired
            return
        }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let snapshot = try await syncEngine.initialSnapshot()
            apply(snapshot)
            transportState = .direct
            try await api?.publishPresence("active")
            await registerPushToken()
            if let directTransport {
                await queue.flush(using: directTransport)
            }
            pendingOfflineCount = await queue.pendingCount()
            lastError = nil
        } catch CompanionTransportError.queuedOffline {
            transportState = .relay
            pendingOfflineCount = await queue.pendingCount()
        } catch {
            transportState = .offline
            lastError = error.localizedDescription
        }
    }

    func sendChat(_ text: String) async {
        guard let api, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return
        }
        let ownerMessage = CompanionConversationMessage(
            id: UUID().uuidString,
            role: "user",
            content: text,
            channel: "iphone",
            createdAt: ISO8601DateFormatter().string(from: Date())
        )
        messages.append(ownerMessage)
        let assistantID = UUID().uuidString
        messages.append(
            CompanionConversationMessage(
                id: assistantID,
                role: "assistant",
                content: "",
                channel: "iphone",
                createdAt: ISO8601DateFormatter().string(from: Date())
            )
        )
        do {
            let stream = await api.streamChat(text)
            for try await event in stream {
                transportState = event.state
                guard let index = messages.firstIndex(
                    where: { $0.id == assistantID }
                ) else {
                    continue
                }
                let current = messages[index]
                let content = event.response?.reply ?? (current.content + event.token)
                messages[index] = CompanionConversationMessage(
                    id: current.id,
                    role: current.role,
                    content: content,
                    channel: current.channel,
                    createdAt: current.createdAt
                )
            }
            let pending = try await api.pendingApprovals()
            let pendingIDs = Set(pending.map(\.id))
            let history = approvals.filter {
                $0.status != "pending" && !pendingIDs.contains($0.id)
            }
            approvals = pending + history
            lastError = nil
        } catch CompanionTransportError.queuedOffline {
            messages.removeAll { $0.id == assistantID && $0.content.isEmpty }
            transportState = .relay
            pendingOfflineCount = await queue.pendingCount()
        } catch {
            messages.removeAll { $0.id == assistantID && $0.content.isEmpty }
            transportState = .offline
            lastError = error.localizedDescription
        }
    }

    func decide(_ approval: CompanionApproval, decision: String) async {
        do {
            try await api?.decide(approval, decision: decision)
            approvals.removeAll { $0.id == approval.id }
            await refresh()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func runRoutine(_ routine: CompanionRoutine) async {
        do {
            try await api?.runRoutine(routine.id)
            await refresh()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func searchMemories(_ query: String) async {
        do {
            memories = try await api?.memories(query: query) ?? []
        } catch {
            lastError = error.localizedDescription
        }
    }

    func saveMemory(_ memory: CompanionMemory) async {
        do {
            let updated = try await api?.updateMemory(memory)
            if let updated, let index = memories.firstIndex(where: { $0.id == updated.id }) {
                memories[index] = updated
            }
        } catch {
            lastError = error.localizedDescription
        }
    }

    func deleteMemory(_ memory: CompanionMemory) async {
        do {
            try await api?.deleteMemory(memory.id)
            memories.removeAll { $0.id == memory.id }
        } catch {
            lastError = error.localizedDescription
        }
    }

    func quickCapture(text: String, type: String = "text") async {
        do {
            try await api?.quickCapture(text: text, captureType: type)
            await refresh()
        } catch CompanionTransportError.queuedOffline {
            transportState = .relay
            pendingOfflineCount = await queue.pendingCount()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func activateKillSwitch() async {
        do {
            if let result = try await api?.activateKillSwitch(
                reason: "iPhone emergency stop"
            ) {
                control = result
            }
        } catch {
            lastError = error.localizedDescription
        }
    }

    func pair(qrPayload: String) async -> Bool {
        do {
            let data: Data
            if qrPayload.hasPrefix("projectq://pair"),
               let components = URLComponents(string: qrPayload),
               let encoded = components.queryItems?.first(
                where: { $0.name == "payload" }
               )?.value,
               let decoded = Data(base64URLEncoded: encoded) {
                data = decoded
            } else {
                data = Data(qrPayload.utf8)
            }
            let qrCode = try JSONDecoder.projectQ.decode(
                PairingQRCode.self,
                from: data
            )
            let session = try await pairingCoordinator.pair(from: qrCode)
            configure(session)
            await refresh()
            return true
        } catch {
            lastError = error.localizedDescription
            return false
        }
    }

    func forgetThisDevice() async {
        try? await credentials.clear()
        syncTask?.cancel()
        api = nil
        syncEngine = nil
        directTransport = nil
        relayTransport = nil
        transportState = .unpaired
        tasks = []
        agents = []
        memories = []
        routines = []
        approvals = []
        audit = []
        messages = []
    }

    func handleDeepLink(_ url: URL) {
        guard url.scheme == "projectq" else { return }
        if url.host == "capture" {
            NotificationCenter.default.post(name: .projectQOpenCapture, object: nil)
        } else if url.host == "approvals" {
            NotificationCenter.default.post(name: .projectQOpenApprovals, object: nil)
        }
    }

    private func configure(_ session: CompanionSession) {
        let sequence = RequestSequenceStore(deviceID: session.deviceID)
        let signer = AuthenticatedRequestSigner(
            deviceID: session.deviceID,
            requestKey: session.requestKey,
            sequenceStore: sequence
        )
        let direct = DirectTransport(
            baseURL: session.directBaseURL,
            signer: signer
        )
        let relay = session.relayBaseURL.flatMap { relayURL in
            session.relayToken.map { token in
                RelayTransport(
                    baseURL: relayURL,
                    bearerToken: token,
                    deviceID: session.deviceID,
                    windowsDeviceID: session.windowsDeviceID,
                    sendKey: session.sendKey,
                    receiveKey: session.receiveKey
                )
            }
        }
        let transport = TransportCoordinator(
            direct: direct,
            relay: relay,
            queue: queue
        )
        let api = CompanionAPI(
            transport: transport,
            approvalSigner: approvalSigner,
            deviceID: session.deviceID
        )
        self.directTransport = direct
        self.relayTransport = relay
        self.api = api
        self.syncEngine = SyncEngine(
            api: api,
            cache: cache,
            cursor: session.receiveCursor
        )
    }

    private func apply(_ snapshot: CompanionSnapshot) {
        tasks = snapshot.tasks
        agents = snapshot.agents
        memories = snapshot.memories
        routines = snapshot.routines
        approvals = snapshot.approvals
        audit = snapshot.audit
        messages = snapshot.conversations.sorted { $0.createdAt < $1.createdAt }
        control = snapshot.control
        let widget = WidgetStatusSnapshot(
            openTaskCount: tasks.filter { $0.status != "completed" }.count,
            runningAgentCount: agents.filter { $0.status == "running" }.count,
            pendingApprovalCount: approvals.count,
            transportState: transportState,
            updatedAt: Date()
        )
        Task {
            try? await cache.write(widget, named: "widget-status")
        }
    }

    private func startPolling() {
        syncTask?.cancel()
        syncTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                guard let self, let engine = self.syncEngine else { return }
                do {
                    let events = try await engine.poll()
                    if !events.isEmpty {
                        await self.refresh()
                    }
                } catch {
                    self.transportState = .offline
                }
            }
        }
    }

    private func registerPushToken() async {
        guard let relayTransport,
              let token = try? await cache.read(
                String.self,
                named: "apns-device-token"
              ),
              !token.isEmpty else {
            return
        }
        let environment = (
            Bundle.main.object(
                forInfoDictionaryKey: "ProjectQAPNsEnvironment"
            ) as? String
        ) ?? "development"
        try? await relayTransport.registerAPNs(
            deviceToken: token,
            environment: environment
        )
    }
}

extension Notification.Name {
    static let projectQOpenCapture = Notification.Name("projectQOpenCapture")
    static let projectQOpenApprovals = Notification.Name("projectQOpenApprovals")
}
