import SwiftUI

struct ApprovalsView: View {
    @EnvironmentObject private var store: CompanionStore

    private var pending: [CompanionApproval] {
        store.approvals.filter { $0.status == "pending" }
    }

    private var completed: [CompanionApproval] {
        store.approvals.filter { $0.status != "pending" }
    }

    var body: some View {
        NavigationStack {
            List {
                if !pending.isEmpty {
                    Section("Pending") {
                        ForEach(pending) { approval in
                            ApprovalRow(approval: approval)
                        }
                    }
                }
                if !completed.isEmpty {
                    Section("Completed") {
                        ForEach(completed) { approval in
                            ApprovalRow(approval: approval)
                        }
                    }
                }
            }
            .navigationTitle("Approvals")
            .refreshable { await store.refresh() }
            .overlay {
                if store.approvals.isEmpty {
                    ContentUnavailableView(
                        "No Pending Approvals",
                        systemImage: "checkmark.shield",
                        description: Text("Sensitive actions waiting for you will appear here.")
                    )
                }
            }
        }
    }
}

private struct ApprovalRow: View {
    let approval: CompanionApproval

    var body: some View {
        NavigationLink {
            ApprovalDetailView(approval: approval)
        } label: {
            VStack(alignment: .leading, spacing: 5) {
                Text(approval.summary)
                HStack {
                    Text("Tier \(approval.actionTier)")
                    Text(approval.toolName)
                    Spacer()
                    Text(approval.status.capitalized)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
    }
}

private struct ApprovalDetailView: View {
    @EnvironmentObject private var store: CompanionStore
    let approval: CompanionApproval

    var body: some View {
        List {
            Section {
                Text(approval.summary)
                LabeledContent("Tool", value: approval.toolName)
                LabeledContent("Tier", value: "\(approval.actionTier)")
                LabeledContent("Expires", value: approval.expiresAt)
            }
            Section("Provenance") {
                Text(approval.originatingGoal)
                Text(approval.inputSources.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Redacted Parameters") {
                Text(String(describing: approval.redactedPreview))
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
            }
            if approval.status == "pending" {
                Section {
                    Button(role: .destructive) {
                        Task { await store.decide(approval, decision: "reject") }
                    } label: {
                        Label("Reject", systemImage: "xmark.circle")
                    }
                    Button {
                        Task { await store.decide(approval, decision: "approve") }
                    } label: {
                        Label("Approve with Face ID", systemImage: "faceid")
                    }
                }
            } else {
                Section("Execution") {
                    LabeledContent("Status", value: approval.status.capitalized)
                    if let outcome = approval.executionOutcome, !outcome.isEmpty {
                        LabeledContent("Outcome", value: outcome)
                    }
                    if let error = approval.executionError, !error.isEmpty {
                        Text(error)
                            .foregroundStyle(.red)
                            .textSelection(.enabled)
                    }
                }
            }
        }
        .navigationTitle("Review Action")
    }
}
