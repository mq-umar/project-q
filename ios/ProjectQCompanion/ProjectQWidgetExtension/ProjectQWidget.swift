import SwiftUI
import WidgetKit

struct ProjectQWidgetEntry: TimelineEntry {
    let date: Date
    let snapshot: WidgetStatusSnapshot
}

struct ProjectQWidgetProvider: TimelineProvider {
    func placeholder(in context: Context) -> ProjectQWidgetEntry {
        entry()
    }

    func getSnapshot(
        in context: Context,
        completion: @escaping (ProjectQWidgetEntry) -> Void
    ) {
        Task { completion(await load()) }
    }

    func getTimeline(
        in context: Context,
        completion: @escaping (Timeline<ProjectQWidgetEntry>) -> Void
    ) {
        Task {
            let current = await load()
            completion(
                Timeline(
                    entries: [current],
                    policy: .after(Date(timeIntervalSinceNow: 15 * 60))
                )
            )
        }
    }

    private func load() async -> ProjectQWidgetEntry {
        let cache = EncryptedCache()
        let snapshot = (try? await cache.read(
            WidgetStatusSnapshot.self,
            named: "widget-status"
        )) ?? entry().snapshot
        return ProjectQWidgetEntry(date: Date(), snapshot: snapshot)
    }

    private func entry() -> ProjectQWidgetEntry {
        ProjectQWidgetEntry(
            date: Date(),
            snapshot: WidgetStatusSnapshot(
                openTaskCount: 0,
                runningAgentCount: 0,
                pendingApprovalCount: 0,
                transportState: .offline,
                updatedAt: Date()
            )
        )
    }
}

struct ProjectQWidgetView: View {
    let entry: ProjectQWidgetEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Project Q")
                    .font(.headline)
                Spacer()
                Image(
                    systemName: entry.snapshot.transportState == .offline
                        ? "wifi.slash"
                        : "link"
                )
            }
            HStack {
                Label("\(entry.snapshot.openTaskCount)", systemImage: "checklist")
                Label("\(entry.snapshot.runningAgentCount)", systemImage: "person.2")
                Label(
                    "\(entry.snapshot.pendingApprovalCount)",
                    systemImage: "checkmark.shield"
                )
            }
            .font(.caption)
            Link(destination: URL(string: "projectq://capture")!) {
                Label("Capture", systemImage: "square.and.pencil")
            }
            .font(.caption)
        }
        .containerBackground(.background, for: .widget)
    }
}

@main
struct ProjectQWidget: Widget {
    let kind = "ProjectQWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(
            kind: kind,
            provider: ProjectQWidgetProvider()
        ) { entry in
            ProjectQWidgetView(entry: entry)
        }
        .configurationDisplayName("Project Q Status")
        .description("Tasks, agents, approvals, and quick capture.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
