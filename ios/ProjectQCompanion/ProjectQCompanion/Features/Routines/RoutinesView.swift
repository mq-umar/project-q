import SwiftUI

struct RoutinesView: View {
    @EnvironmentObject private var store: CompanionStore

    var body: some View {
        NavigationStack {
            List(store.routines) { routine in
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text(routine.name)
                        if routine.trusted {
                            Image(systemName: "checkmark.seal.fill")
                                .foregroundStyle(.green)
                                .accessibilityLabel("Trusted")
                        }
                    }
                    Text(routine.description.isEmpty ? routine.goal : routine.description)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if let outcome = routine.lastRunOutcome {
                        LabeledContent("Last run", value: outcome.capitalized)
                            .font(.caption)
                    }
                    Button {
                        Task { await store.runRoutine(routine) }
                    } label: {
                        Label("Run", systemImage: "play.fill")
                    }
                    .buttonStyle(.bordered)
                }
                .padding(.vertical, 4)
            }
            .navigationTitle("Routines")
        }
    }
}
