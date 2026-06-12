import Foundation
import UserNotifications

actor NotificationManager {
    static let shared = NotificationManager()

    func requestAuthorization() async throws {
        let center = UNUserNotificationCenter.current()
        let approvalActions = [
            UNNotificationAction(
                identifier: "OPEN_APPROVAL",
                title: "Review",
                options: [.foreground, .authenticationRequired]
            )
        ]
        center.setNotificationCategories([
            UNNotificationCategory(
                identifier: "PROJECT_Q_APPROVAL",
                actions: approvalActions,
                intentIdentifiers: []
            ),
            UNNotificationCategory(
                identifier: "PROJECT_Q_ACTIVITY",
                actions: [],
                intentIdentifiers: []
            ),
        ])
        _ = try await center.requestAuthorization(
            options: [.alert, .badge, .sound]
        )
    }

    func deepLink(for response: UNNotificationResponse) -> URL? {
        let userInfo = response.notification.request.content.userInfo
        guard let route = userInfo["route"] as? String else {
            return nil
        }
        return URL(string: "projectq://\(route)")
    }
}
