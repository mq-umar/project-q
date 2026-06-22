import Foundation

enum JSONValue: Codable, Hashable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            self = .array(try container.decode([JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }
}

struct CompanionSession: Codable, Sendable {
    var directBaseURL: URL
    var relayBaseURL: URL?
    var relayToken: String?
    var deviceID: String
    var windowsDeviceID: String
    var sendKey: Data
    var receiveKey: Data
    var requestKey: Data
    var receiveCursor: Int64
}

struct PairingOffer: Codable, Sendable {
    let protocolVersion: Int
    let deviceID: String
    let windowsDeviceID: String
    let windowsKeyAgreementPublicKey: String
    let pairingToken: String
    let expiresAt: String
}

struct PairingQRCode: Codable, Sendable {
    let directBaseURL: URL
    let relayBaseURL: URL?
    let relayToken: String?
    let offer: PairingOffer
}

struct PairingCompletion: Codable, Sendable {
    let protocolVersion: Int
    let deviceID: String
    let windowsDeviceID: String
    let status: String
    let pairedAt: String
}

struct CompanionEnvelope: Codable, Hashable, Sendable {
    let protocolVersion: Int
    let messageID: String
    let senderDeviceID: String
    let recipientDeviceID: String
    let eventKind: String
    let createdAt: String
    let sequenceNumber: Int64
    let nonce: String
    let ciphertext: String
    let tag: String
    let contentType: String
    let algorithm: String
}

struct CompanionTask: Identifiable, Codable, Hashable, Sendable {
    let id: String
    let title: String
    let description: String
    let priority: Int
    let status: String
    let dueAt: String?
    let updatedAt: String
}

struct CompanionAgent: Identifiable, Codable, Hashable, Sendable {
    let id: String
    let name: String
    let agentType: String
    let goal: String
    let status: String
    let lastRunAt: String?
    let lastRunOutcome: String?
    let lastRunMode: String?
}

struct CompanionMemory: Identifiable, Codable, Hashable, Sendable {
    let id: String
    var text: String
    var kind: String
    let source: String
    var confidence: Double
    var ownerConfirmed: Bool
    var tags: [String]
    let updatedAt: String
}

struct CompanionRoutine: Identifiable, Codable, Hashable, Sendable {
    let id: String
    let name: String
    let goal: String
    let description: String
    let status: String
    let trusted: Bool
    let lastRunAt: String?
    let lastRunOutcome: String?
    let updatedAt: String
}

struct CompanionApproval: Identifiable, Codable, Hashable, Sendable {
    let id: String
    let actionTier: Int
    let toolName: String
    let summary: String
    let redactedPreview: [String: JSONValue]
    let payloadDigest: String
    let originatingGoal: String
    let inputSources: [String]
    let status: String
    let expiresAt: String
    let createdAt: String
    let updatedAt: String?
    let executedAt: String?
    let executionOutcome: String?
    let executionError: String?
}

struct CompanionAuditEntry: Identifiable, Codable, Hashable, Sendable {
    let id: String
    let timestamp: String
    let actionType: String
    let actionTier: Int
    let toolName: String
    let model: String
    let inputSources: [String]
    let approvedByOwner: Bool
    let outcome: String
    let error: String
    let metadata: [String: JSONValue]
}

struct CompanionConversationMessage: Identifiable, Codable, Hashable, Sendable {
    let id: String
    let role: String
    let content: String
    let channel: String
    let createdAt: String
}

struct CompanionControlStatus: Codable, Hashable, Sendable {
    let active: Bool
    let reason: String
    let activatedAt: String
    let source: String
}

struct CompanionSnapshot: Codable, Sendable {
    let deviceID: String
    let snapshotSequence: Int64
    let acknowledgedSequence: Int64
    let conversations: [CompanionConversationMessage]
    let memories: [CompanionMemory]
    let tasks: [CompanionTask]
    let agents: [CompanionAgent]
    let routines: [CompanionRoutine]
    let approvals: [CompanionApproval]
    let audit: [CompanionAuditEntry]
    let control: CompanionControlStatus
}

struct CompanionSyncEvent: Identifiable, Codable, Sendable {
    let id: String
    let sequenceNumber: Int64
    let resourceType: String
    let resourceID: String
    let operation: String
    let payload: [String: JSONValue]
    let createdAt: String
}

struct CompanionSyncPage: Codable, Sendable {
    let items: [CompanionSyncEvent]
    let nextSequence: Int64
    let acknowledgedSequence: Int64
    let hasMore: Bool
    let snapshotRequired: Bool
}

struct CompanionChatResponse: Codable, Sendable {
    let reply: String
    let createdActionRequestIDs: [String]
}

struct CompanionChatStreamEvent: Sendable {
    let token: String
    let response: CompanionChatResponse?
    let state: CompanionTransportState
}

struct ItemsResponse<Item: Codable & Sendable>: Codable, Sendable {
    let items: [Item]
}

enum CompanionTransportState: String, Codable, Sendable {
    case direct
    case relay
    case offline
    case unpaired
}

struct OfflineCommand: Identifiable, Codable, Sendable {
    let id: UUID
    let method: String
    let path: String
    let body: Data?
    let createdAt: Date
    var attemptCount: Int
}

struct WidgetStatusSnapshot: Codable, Sendable {
    let openTaskCount: Int
    let runningAgentCount: Int
    let pendingApprovalCount: Int
    let transportState: CompanionTransportState
    let updatedAt: Date
}
