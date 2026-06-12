import Foundation

actor CompanionAPI {
    private let transport: any CompanionTransport
    private let approvalSigner: BiometricApprovalSigner
    private let deviceID: String

    init(
        transport: any CompanionTransport,
        approvalSigner: BiometricApprovalSigner,
        deviceID: String
    ) {
        self.transport = transport
        self.approvalSigner = approvalSigner
        self.deviceID = deviceID
    }

    func snapshot() async throws -> CompanionSnapshot {
        try await request("GET", "/api/companion/v2/sync/snapshot")
    }

    func events(after sequence: Int64) async throws -> CompanionSyncPage {
        try await request(
            "GET",
            "/api/companion/v2/sync/events?after_sequence=\(sequence)&limit=200"
        )
    }

    func acknowledge(_ sequence: Int64) async throws {
        let _: JSONValue = try await request(
            "POST",
            "/api/companion/v2/sync/ack",
            body: ["sequence_number": sequence]
        )
    }

    func chat(_ message: String) async throws -> CompanionChatResponse {
        try await request(
            "POST",
            "/api/companion/v2/chat",
            body: ["message": message]
        )
    }

    func streamChat(
        _ message: String
    ) -> AsyncThrowingStream<CompanionChatStreamEvent, Error> {
        let transport = self.transport
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let body = try JSONEncoder.projectQ.encode(
                        ["message": message]
                    )
                    let stream = await transport.streamChat(
                        body: body,
                        commandID: UUID()
                    )
                    for try await event in stream {
                        continuation.yield(event)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    func pendingApprovals() async throws -> [CompanionApproval] {
        let response: ItemsResponse<CompanionApproval> = try await request(
            "GET",
            "/api/companion/v2/approvals"
        )
        return response.items
    }

    func decide(
        _ approval: CompanionApproval,
        decision: String
    ) async throws {
        let timestamp = ISO8601DateFormatter().string(from: Date())
        let message = Data(
            """
            PROJECTQ-APPROVAL-V2
            \(approval.id)
            \(decision)
            \(approval.actionTier)
            \(approval.payloadDigest)
            \(approval.expiresAt)
            \(deviceID)
            \(timestamp)
            """.utf8
        )
        let signature = try await approvalSigner.sign(
            message,
            reason: "\(decision.capitalized) \(approval.summary)"
        )
        let body: [String: JSONValue] = [
            "decision": .string(decision),
            "timestamp": .string(timestamp),
            "signature": .string(signature),
            "biometric_backed": .bool(true),
        ]
        let _: JSONValue = try await request(
            "POST",
            "/api/companion/v2/approvals/\(approval.id)/decision",
            body: body
        )
    }

    func routines() async throws -> [CompanionRoutine] {
        let response: ItemsResponse<CompanionRoutine> = try await request(
            "GET",
            "/api/companion/v2/routines"
        )
        return response.items
    }

    func runRoutine(_ id: String) async throws {
        let _: JSONValue = try await request(
            "POST",
            "/api/companion/v2/routines/\(id)/run",
            body: [String: String]()
        )
    }

    func memories(query: String = "") async throws -> [CompanionMemory] {
        let encoded = query.addingPercentEncoding(
            withAllowedCharacters: .urlQueryAllowed
        ) ?? ""
        let suffix = encoded.isEmpty ? "" : "?q=\(encoded)"
        let response: ItemsResponse<CompanionMemory> = try await request(
            "GET",
            "/api/companion/v2/memories\(suffix)"
        )
        return response.items
    }

    func updateMemory(_ memory: CompanionMemory) async throws -> CompanionMemory {
        try await request(
            "PUT",
            "/api/companion/v2/memories/\(memory.id)",
            body: [
                "text": JSONValue.string(memory.text),
                "kind": .string(memory.kind),
                "confidence": .number(memory.confidence),
                "owner_confirmed": .bool(memory.ownerConfirmed),
                "tags": .array(memory.tags.map(JSONValue.string)),
            ]
        )
    }

    func deleteMemory(_ id: String) async throws {
        let _: JSONValue = try await request(
            "DELETE",
            "/api/companion/v2/memories/\(id)"
        )
    }

    func quickCapture(
        text: String,
        captureType: String = "text"
    ) async throws {
        let _: JSONValue = try await request(
            "POST",
            "/api/companion/v2/quick-capture",
            body: [
                "text": JSONValue.string(text),
                "capture_type": .string(captureType),
                "metadata": .object([:]),
            ]
        )
    }

    func publishPresence(_ state: String) async throws {
        let _: JSONValue = try await request(
            "POST",
            "/api/companion/v2/presence",
            body: [
                "state": JSONValue.string(state),
                "ttl_seconds": .number(120),
            ]
        )
    }

    func activateKillSwitch(reason: String) async throws -> CompanionControlStatus {
        try await request(
            "POST",
            "/api/companion/v2/control/kill-switch",
            body: ["reason": reason]
        )
    }

    private func request<Response: Decodable>(
        _ method: String,
        _ path: String
    ) async throws -> Response {
        let result = try await transport.send(method: method, path: path, body: nil)
        return try JSONDecoder.projectQ.decode(Response.self, from: result.data)
    }

    private func request<Response: Decodable, Body: Encodable>(
        _ method: String,
        _ path: String,
        body: Body
    ) async throws -> Response {
        let encoded = try JSONEncoder.projectQ.encode(body)
        let result = try await transport.send(
            method: method,
            path: path,
            body: encoded
        )
        return try JSONDecoder.projectQ.decode(Response.self, from: result.data)
    }
}
