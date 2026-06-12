import BackgroundTasks
import Foundation

enum BackgroundSync {
    static let refreshIdentifier = "com.projectq.companion.refresh"
    static let processingIdentifier = "com.projectq.companion.processing"

    static func register() {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: refreshIdentifier,
            using: nil
        ) { task in
            guard let refreshTask = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            handle(refreshTask)
        }
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: processingIdentifier,
            using: nil
        ) { task in
            guard let processingTask = task as? BGProcessingTask else {
                task.setTaskCompleted(success: false)
                return
            }
            handle(processingTask)
        }
    }

    static func schedule() {
        let refresh = BGAppRefreshTaskRequest(identifier: refreshIdentifier)
        refresh.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(refresh)

        let processing = BGProcessingTaskRequest(identifier: processingIdentifier)
        processing.requiresNetworkConnectivity = true
        processing.requiresExternalPower = false
        processing.earliestBeginDate = Date(timeIntervalSinceNow: 30 * 60)
        try? BGTaskScheduler.shared.submit(processing)
    }

    private static func handle(_ task: BGTask) {
        schedule()
        let worker = Task {
            let store = await MainActor.run { CompanionStore.live() }
            await store.start()
            await store.refresh()
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = {
            worker.cancel()
            task.setTaskCompleted(success: false)
        }
    }
}
