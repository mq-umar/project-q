import SwiftUI

@main
struct ProjectQCompanionApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = CompanionStore.live()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(store)
                .task {
                    await store.start()
                }
                .onOpenURL { url in
                    store.handleDeepLink(url)
                }
        }
    }
}
