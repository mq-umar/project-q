import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var viewModel: CompanionViewModel

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let status = viewModel.status {
                        SummaryCard(
                            title: status.project,
                            subtitle: "Status: \(status.status)",
                            detail: "\(status.toolCount) registered tools"
                        )
                    }

                    SummaryCard(
                        title: "Tasks",
                        subtitle: "\(viewModel.tasks.count) tracked",
                        detail: "Live status mirrored from the Windows runtime"
                    )

                    SummaryCard(
                        title: "Agents",
                        subtitle: "\(viewModel.agents.count) registered",
                        detail: "Future versions will support approvals and remote triggers here"
                    )
                }
                .padding()
            }
            .navigationTitle("Project Q")
            .task {
                await viewModel.refresh()
            }
        }
    }
}

private struct SummaryCard: View {
    let title: String
    let subtitle: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.headline)
            Text(subtitle)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Text(detail)
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 18))
    }
}

