import UIKit
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [
            UIApplication.LaunchOptionsKey: Any
        ]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        BackgroundSync.register()
        BackgroundSync.schedule()
        Task {
            try? await NotificationManager.shared.requestAuthorization()
            await MainActor.run {
                application.registerForRemoteNotifications()
            }
        }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task {
            let cache = EncryptedCache()
            try? await cache.write(
                deviceToken.map { String(format: "%02x", $0) }.joined(),
                named: "apns-device-token"
            )
        }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        if let url = await NotificationManager.shared.deepLink(for: response) {
            await UIApplication.shared.open(url)
        }
    }
}
