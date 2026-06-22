import SwiftUI

struct MoreView: View {
    @EnvironmentObject private var store: CompanionStore

    var body: some View {
        NavigationStack {
            List {
                if store.transportState == .unpaired {
                    NavigationLink {
                        PairingView()
                    } label: {
                        Label("Pair This iPhone", systemImage: "qrcode.viewfinder")
                    }
                }
                NavigationLink {
                    MemoryBrowserView()
                } label: {
                    Label("Memory", systemImage: "brain.head.profile")
                }
                NavigationLink {
                    QuickCaptureView()
                } label: {
                    Label("Quick Capture", systemImage: "square.and.pencil")
                }
                NavigationLink {
                    AuditLogView()
                } label: {
                    Label("Audit Log", systemImage: "list.bullet.clipboard")
                }
                Section("Companion") {
                    LabeledContent("Transport", value: store.transportState.rawValue.capitalized)
                    LabeledContent("Offline Queue", value: "\(store.pendingOfflineCount)")
                    if let error = store.lastError {
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                    if store.transportState != .unpaired {
                        Button("Forget This Device", role: .destructive) {
                            Task { await store.forgetThisDevice() }
                        }
                    }
                }
            }
            .navigationTitle("More")
        }
    }
}
