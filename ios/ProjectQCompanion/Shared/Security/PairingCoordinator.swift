import CryptoKit
import Foundation

private struct PairingCompletionRequest: Encodable {
    let deviceID: String
    let pairingToken: String
    let keyAgreementPublicKey: String
    let approvalPublicKey: String
}

actor PairingCoordinator {
    private let credentials: CredentialStore
    private let approvalSigner: BiometricApprovalSigner
    private let urlSession: URLSession

    init(
        credentials: CredentialStore = CredentialStore(),
        approvalSigner: BiometricApprovalSigner = BiometricApprovalSigner(),
        urlSession: URLSession = .shared
    ) {
        self.credentials = credentials
        self.approvalSigner = approvalSigner
        self.urlSession = urlSession
    }

    func pair(from qrCode: PairingQRCode) async throws -> CompanionSession {
        let privateKey = CompanionCrypto.generatePrivateKey()
        let phonePublicKey = privateKey.publicKey.rawRepresentation
            .base64URLEncodedString()
        let approvalPublicKey = try await approvalSigner.publicKey()
        let body = PairingCompletionRequest(
            deviceID: qrCode.offer.deviceID,
            pairingToken: qrCode.offer.pairingToken,
            keyAgreementPublicKey: phonePublicKey,
            approvalPublicKey: approvalPublicKey
        )
        let url = qrCode.directBaseURL
            .appendingPathComponent("api/companion/v2/pairing/complete")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder.projectQ.encode(body)
        let (data, response) = try await urlSession.data(for: request)
        guard let http = response as? HTTPURLResponse, 200..<300 ~= http.statusCode else {
            throw CompanionTransportError.server(
                (response as? HTTPURLResponse)?.statusCode ?? 0,
                String(data: data, encoding: .utf8) ?? ""
            )
        }
        _ = try JSONDecoder.projectQ.decode(PairingCompletion.self, from: data)
        guard let pairingToken = Data(
            base64URLEncoded: qrCode.offer.pairingToken
        ) else {
            throw CompanionCryptoError.invalidEncoding
        }
        let keys = try CompanionCrypto.deriveSessionKeys(
            privateKey: privateKey,
            peerPublicKey: qrCode.offer.windowsKeyAgreementPublicKey,
            pairingToken: pairingToken,
            localDeviceID: qrCode.offer.deviceID,
            remoteDeviceID: qrCode.offer.windowsDeviceID
        )
        let session = CompanionSession(
            directBaseURL: qrCode.directBaseURL,
            relayBaseURL: qrCode.relayBaseURL,
            relayToken: qrCode.relayToken,
            deviceID: qrCode.offer.deviceID,
            windowsDeviceID: qrCode.offer.windowsDeviceID,
            sendKey: keys.sendKey,
            receiveKey: keys.receiveKey,
            requestKey: keys.requestKey,
            receiveCursor: 0
        )
        try await credentials.save(session)
        return session
    }
}
