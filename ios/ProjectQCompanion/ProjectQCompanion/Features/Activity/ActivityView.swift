import SwiftUI

struct ActivityView: View {
    @EnvironmentObject private var store: CompanionStore

    var body: some View {
        NavigationStack {
            List {
                Section {
                    LabeledContent("Connection", value: store.transportState.rawValue.capitalized)
                    LabeledContent("Queued", value: "\(store.pendingOfflineCount)")
                    if store.control.active {
                        Label(store.control.reason, systemImage: "exclamationmark.octagon.fill")
                            .foregroundStyle(.red)
                    } else {
                        Button(role: .destructive) {
                            Task { await store.activateKillSwitch() }
                        } label: {
                            Label("Emergency Stop", systemImage: "stop.circle.fill")
                        }
                    }
                }

                Section("Tasks") {
                    ForEach(store.tasks) { task in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(task.title)
                            Text(task.status.replacingOccurrences(of: "_", with: " ").capitalized)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                Section("Agents") {
                    ForEach(store.agents) { agent in
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(agent.name)
                                Text(agent.goal)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(2)
                                if let outcome = agent.lastRunOutcome {
                                    Text("Last run: \(outcome.capitalized)")
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            if agent.status == "running" {
                                ProgressView()
                                    .accessibilityLabel("Agent running")
                            } else {
                                Text(agent.status.capitalized)
                                    .font(.caption)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Activity")
            .refreshable { await store.refresh() }
            .overlay {
                if store.tasks.isEmpty && store.agents.isEmpty {
                    ContentUnavailableView(
                        "No Active Work",
                        systemImage: "waveform.path.ecg",
                        description: Text("Tasks and agent progress will appear here.")
                    )
                }
            }
        }
    }
}
