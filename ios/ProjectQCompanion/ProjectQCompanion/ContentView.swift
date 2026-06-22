import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            ActivityView()
                .tabItem {
                    Label("Activity", systemImage: "waveform.path.ecg")
                }

            ChatView()
                .tabItem {
                    Label("Chat", systemImage: "bubble.left.and.bubble.right")
                }

            ApprovalsView()
                .tabItem {
                    Label("Approvals", systemImage: "checkmark.shield")
                }

            RoutinesView()
                .tabItem {
                    Label("Routines", systemImage: "play.square.stack")
                }

            MoreView()
                .tabItem {
                    Label("More", systemImage: "ellipsis.circle")
                }
        }
    }
}
