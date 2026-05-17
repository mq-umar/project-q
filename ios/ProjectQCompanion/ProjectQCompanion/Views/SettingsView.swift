import SwiftUI

struct SettingsView: View {
    var body: some View {
        NavigationStack {
            Form {
                Section("Companion") {
                    Toggle("Require Face ID for approvals", isOn: .constant(true))
                    Toggle("Enable push notifications", isOn: .constant(true))
                }

                Section("Roadmap") {
                    Text("Remote approvals, quick capture, and encrypted relay wiring land in the next phase.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Settings")
        }
    }
}
