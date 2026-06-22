import CryptoKit
import Foundation
import LocalAuthentication
import Security

actor BiometricApprovalSigner {
    private let keychain: KeychainStore
    private let keyAccount = "approval-secure-enclave-key"

    init(keychain: KeychainStore = KeychainStore()) {
        self.keychain = keychain
    }

    func publicKey() throws -> String {
        let key = try loadOrCreateKey(context: nil)
        return key.publicKey.x963Representation.base64URLEncodedString()
    }

    func sign(_ message: Data, reason: String) async throws -> String {
        let context = LAContext()
        context.localizedReason = reason
        let key = try loadOrCreateKey(context: context)
        let signature = try key.signature(for: message)
        return signature.derRepresentation.base64URLEncodedString()
    }

    private func loadOrCreateKey(
        context: LAContext?
    ) throws -> SecureEnclave.P256.Signing.PrivateKey {
        if let representation = try keychain.data(for: keyAccount) {
            return try SecureEnclave.P256.Signing.PrivateKey(
                dataRepresentation: representation,
                authenticationContext: context
            )
        }
        var error: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(
            nil,
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            [.privateKeyUsage, .biometryCurrentSet],
            &error
        ) else {
            throw error!.takeRetainedValue() as Error
        }
        let key = try SecureEnclave.P256.Signing.PrivateKey(
            accessControl: access,
            authenticationContext: context
        )
        try keychain.set(key.dataRepresentation, for: keyAccount)
        return key
    }
}
