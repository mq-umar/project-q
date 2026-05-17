import Foundation

struct QTask: Identifiable, Codable {
    let id: String
    let title: String
    let status: String
    let priority: Int
    let updatedAt: String
}

struct QMemory: Identifiable, Codable {
    let id: String
    let text: String
    let kind: String
    let confidence: Double
    let updatedAt: String
}

struct QAgent: Identifiable, Codable {
    let id: String
    let name: String
    let agentType: String
    let goal: String
    let status: String
}

struct QAuditEvent: Identifiable, Codable {
    let id: String
    let actionType: String
    let actionTier: Int
    let toolName: String
    let outcome: String
    let timestamp: String
}

struct QStatus: Codable {
    let project: String
    let status: String
    let toolCount: Int
}

