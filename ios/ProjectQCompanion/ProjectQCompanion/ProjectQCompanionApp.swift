import SwiftUI

@main
struct ProjectQCompanionApp: App {
    @StateObject private var viewModel = CompanionViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(viewModel)
        }
    }
}

