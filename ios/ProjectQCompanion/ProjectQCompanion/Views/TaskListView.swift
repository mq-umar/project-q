import SwiftUI

struct TaskListView: View {
    @EnvironmentObject private var viewModel: CompanionViewModel

    var body: some View {
        NavigationStack {
            List(viewModel.tasks) { task in
                VStack(alignment: .leading, spacing: 4) {
                    Text(task.title)
                        .font(.headline)
                    Text("\(task.status) · priority \(task.priority)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Tasks")
        }
    }
}

