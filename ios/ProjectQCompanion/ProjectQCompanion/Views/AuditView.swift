import SwiftUI

struct AuditView: View {
    @EnvironmentObject private var viewModel: CompanionViewModel

    var body: some View {
        NavigationStack {
            List(viewModel.auditEvents) { event in
                VStack(alignment: .leading, spacing: 4) {
                    Text(event.actionType)
                        .font(.headline)
                    Text("\(event.toolName) · \(event.outcome)")
                        .font(.subheadline)
                    Text("Tier \(event.actionTier) · \(event.timestamp)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Audit")
        }
    }
}

