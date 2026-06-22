import SwiftUI

struct MemoryBrowserView: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var access = SensitiveAccessController()
    @State private var query = ""
    @State private var editing: CompanionMemory?
    @State private var deleting: CompanionMemory?

    var body: some View {
        ZStack {
            List(store.memories) { memory in
            Button {
                editing = memory
            } label: {
                VStack(alignment: .leading, spacing: 4) {
                    Text(memory.text)
                        .foregroundStyle(.primary)
                    Text("\(memory.kind) - \(memory.source)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .swipeActions {
                Button(role: .destructive) {
                    deleting = memory
                } label: {
                    Label("Delete", systemImage: "trash")
                }
            }
        }
        .navigationTitle("Memory")
        .searchable(text: $query)
        .onSubmit(of: .search) {
            Task { await store.searchMemories(query) }
        }
        .sheet(item: $editing) { memory in
            MemoryEditor(memory: memory)
        }
        .confirmationDialog(
            "Delete this memory?",
            isPresented: Binding(
                get: { deleting != nil },
                set: { if !$0 { deleting = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Delete", role: .destructive) {
                guard let deleting else { return }
                Task { await store.deleteMemory(deleting) }
                self.deleting = nil
            }
        }
            if !access.isUnlocked {
                SensitiveAccessPrompt(
                    title: "Memory Locked",
                    message: access.errorMessage,
                    isAuthenticating: access.isAuthenticating
                ) {
                    await access.unlock(
                        reason: "View and edit your Project Q memory."
                    )
                }
            }
        }
        .task {
            await access.unlock(reason: "View and edit your Project Q memory.")
        }
    }
}

private struct MemoryEditor: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State var memory: CompanionMemory

    var body: some View {
        NavigationStack {
            Form {
                TextEditor(text: $memory.text)
                    .frame(minHeight: 180)
                TextField("Kind", text: $memory.kind)
                Slider(value: $memory.confidence, in: 0...1)
                Toggle("Owner confirmed", isOn: $memory.ownerConfirmed)
            }
            .navigationTitle("Edit Memory")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Task {
                            await store.saveMemory(memory)
                            dismiss()
                        }
                    }
                }
            }
        }
    }
}
