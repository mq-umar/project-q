import LocalAuthentication
import SwiftUI

@MainActor
final class SensitiveAccessController: ObservableObject {
    @Published private(set) var isUnlocked = false
    @Published private(set) var isAuthenticating = false
    @Published private(set) var errorMessage: String?

    func unlock(reason: String) async {
        guard !isUnlocked, !isAuthenticating else { return }
        isAuthenticating = true
        errorMessage = nil
        defer { isAuthenticating = false }

        let context = LAContext()
        context.localizedCancelTitle = "Cancel"
        var policyError: NSError?
        guard context.canEvaluatePolicy(
            .deviceOwnerAuthenticationWithBiometrics,
            error: &policyError
        ) else {
            errorMessage = policyError?.localizedDescription
                ?? "Face ID or Touch ID is unavailable."
            return
        }
        do {
            isUnlocked = try await context.evaluatePolicy(
                .deviceOwnerAuthenticationWithBiometrics,
                localizedReason: reason
            )
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

struct SensitiveAccessPrompt: View {
    let title: String
    let message: String?
    let isAuthenticating: Bool
    let unlock: () async -> Void

    var body: some View {
        VStack(spacing: 16) {
            ContentUnavailableView(
                title,
                systemImage: "faceid",
                description: Text(
                    message ?? "Authenticate with Face ID or Touch ID to continue."
                )
            )
            Button {
                Task { await unlock() }
            } label: {
                Label("Unlock", systemImage: "faceid")
            }
            .buttonStyle(.borderedProminent)
            .disabled(isAuthenticating)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
        .background(Color(uiColor: .systemBackground))
    }
}
