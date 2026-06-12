import SwiftUI

struct AuditLogView: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var access = SensitiveAccessController()

    var body: some View {
        ZStack {
            List(store.audit) { entry in
            NavigationLink {
                AuditEntryDetailView(entry: entry)
            } label: {
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Text(entry.actionType.replacingOccurrences(of: "_", with: " "))
                            .lineLimit(1)
                        Spacer()
                        Text("Tier \(entry.actionTier)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    HStack {
                        Text(entry.toolName)
                        Spacer()
                        Text(entry.outcome.capitalized)
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("Audit Log")
        .refreshable { await store.refresh() }
        .overlay {
            if store.audit.isEmpty {
                ContentUnavailableView(
                    "No Audit Entries",
                    systemImage: "list.bullet.clipboard",
                    description: Text("Recent Windows actions will appear here.")
                )
            }
            if !access.isUnlocked {
                SensitiveAccessPrompt(
                    title: "Audit Log Locked",
                    message: access.errorMessage,
                    isAuthenticating: access.isAuthenticating
                ) {
                    await access.unlock(
                        reason: "Review your private Project Q audit history."
                    )
                }
            }
        }
        .task {
            await access.unlock(
                reason: "Review your private Project Q audit history."
            )
        }
    }
}

private struct AuditEntryDetailView: View {
    let entry: CompanionAuditEntry

    var body: some View {
        List {
            Section {
                LabeledContent("Action", value: entry.actionType)
                LabeledContent("Tool", value: entry.toolName)
                LabeledContent("Tier", value: "\(entry.actionTier)")
                LabeledContent("Outcome", value: entry.outcome)
                LabeledContent("Time", value: entry.timestamp)
            }
            Section("Decision Context") {
                LabeledContent("Model", value: entry.model)
                LabeledContent(
                    "Owner approved",
                    value: entry.approvedByOwner ? "Yes" : "No"
                )
                Text(entry.inputSources.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if !entry.metadata.isEmpty {
                Section("Redacted Metadata") {
                    Text(String(describing: entry.metadata))
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                }
            }
            if !entry.error.isEmpty {
                Section("Error") {
                    Text(entry.error)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                }
            }
        }
        .navigationTitle("Audit Entry")
    }
}
