import SwiftUI

struct AgentListView: View {
    @EnvironmentObject private var viewModel: CompanionViewModel

    var body: some View {
        NavigationStack {
            List(viewModel.agents) { agent in
                VStack(alignment: .leading, spacing: 4) {
                    Text(agent.name)
                        .font(.headline)
                    Text(agent.goal)
                        .font(.subheadline)
                    Text("\(agent.agentType) · \(agent.status)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Agents")
        }
    }
}

