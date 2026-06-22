import Foundation

struct RelayCommandPayload: Codable, Sendable {
    let commandID: UUID
    let method: String
    let path: String
    let body: Data
}

private struct RelayResponsePayload: Codable, Sendable {
    let requestMessageID: String
    let statusCode: Int
    let body: Data
}

private struct RelayInboxResponse: Codable, Sendable {
    let items: [CompanionEnvelope]
    let hasMore: Bool
}

private struct APNsRegistrationPayload: Codable, Sendable {
    let deviceToken: String
    let environment: String
}

actor RelayTransport: CompanionTransport {
    private let baseURL: URL
    private let bearerToken: String
    private let deviceID: String
    private let windowsDeviceID: String
    private let sendKey: Data
    private let receiveKey: Data
    private let sequenceStore: RequestSequenceStore
    private let receiveSequenceStore: RequestSequenceStore
    private let session: URLSession
    private let responseTimeout: Duration
    private var responsesByRequestID: [String: RelayResponsePayload] = [:]

    init(
        baseURL: URL,
        bearerToken: String,
        deviceID: String,
        windowsDeviceID: String,
        sendKey: Data,
        receiveKey: Data,
        sequenceStore: RequestSequenceStore? = nil,
        receiveSequenceStore: RequestSequenceStore? = nil,
        session: URLSession = .shared,
        responseTimeout: Duration = .seconds(30)
    ) {
        self.baseURL = baseURL
        self.bearerToken = bearerToken
        self.deviceID = deviceID
        self.windowsDeviceID = windowsDeviceID
        self.sendKey = sendKey
        self.receiveKey = receiveKey
        self.sequenceStore = sequenceStore ?? RequestSequenceStore(
            deviceID: deviceID,
            namespace: "relay-send-sequence"
        )
        self.receiveSequenceStore = receiveSequenceStore ?? RequestSequenceStore(
            deviceID: deviceID,
            namespace: "relay-receive-sequence"
        )
        self.session = session
        self.responseTimeout = responseTimeout
    }

    func send(
        method: String,
        path: String,
        body: Data?,
        commandID: UUID
    ) async throws -> CompanionTransportResult {
        let sequence = try await sequenceStore.next()
        let envelope = try CompanionCrypto.seal(
            RelayCommandPayload(
                commandID: commandID,
                method: method,
                path: path,
                body: body ?? Data()
            ),
            key: sendKey,
            senderDeviceID: deviceID,
            recipientDeviceID: windowsDeviceID,
            eventKind: "companion_command",
            sequenceNumber: sequence
        )
        try await upload(envelope)

        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: responseTimeout)
        while clock.now < deadline {
            if let payload = responsesByRequestID.removeValue(
                forKey: envelope.messageID
            ) {
                return try result(from: payload)
            }
            let inbox = try await pull()
            for candidate in inbox.items.sorted(
                by: { $0.sequenceNumber < $1.sequenceNumber }
            )
            where candidate.eventKind == "companion_response" {
                let payload = try CompanionCrypto.open(
                    candidate,
                    key: receiveKey,
                    expectedSender: windowsDeviceID,
                    expectedRecipient: deviceID,
                    as: RelayResponsePayload.self
                )
                guard try await receiveSequenceStore.accept(
                    candidate.sequenceNumber
                ) else {
                    try await acknowledge(candidate.messageID)
                    continue
                }
                try await acknowledge(candidate.messageID)
                responsesByRequestID[payload.requestMessageID] = payload
            }
            if let payload = responsesByRequestID.removeValue(
                forKey: envelope.messageID
            ) {
                return try result(from: payload)
            }
            try await Task.sleep(for: .milliseconds(250))
        }
        throw CompanionTransportError.unavailable
    }

    private func result(
        from payload: RelayResponsePayload
    ) throws -> CompanionTransportResult {
        guard 200..<300 ~= payload.statusCode else {
            throw CompanionTransportError.server(
                payload.statusCode,
                String(data: payload.body, encoding: .utf8) ?? ""
            )
        }
        return CompanionTransportResult(
            data: payload.body,
            state: .relay,
            queued: false
        )
    }

    private func upload(_ envelope: CompanionEnvelope) async throws {
        var request = authorizedRequest(
            url: baseURL
                .appendingPathComponent("v1")
                .appendingPathComponent("envelopes"),
            method: "POST"
        )
        request.httpBody = try JSONEncoder.projectQ.encode(envelope)
        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse,
              200..<300 ~= http.statusCode else {
            throw CompanionTransportError.unavailable
        }
    }

    func registerAPNs(
        deviceToken: String,
        environment: String
    ) async throws {
        var request = authorizedRequest(
            url: baseURL
                .appendingPathComponent("v1")
                .appendingPathComponent("apns")
                .appendingPathComponent("register"),
            method: "POST"
        )
        request.httpBody = try JSONEncoder.projectQ.encode(
            APNsRegistrationPayload(
                deviceToken: deviceToken,
                environment: environment
            )
        )
        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse,
              200..<300 ~= http.statusCode else {
            throw CompanionTransportError.unavailable
        }
    }

    private func pull() async throws -> RelayInboxResponse {
        let request = authorizedRequest(
            url: baseURL
                .appendingPathComponent("v1")
                .appendingPathComponent("envelopes"),
            method: "GET"
        )
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse,
              200..<300 ~= http.statusCode else {
            throw CompanionTransportError.unavailable
        }
        return try JSONDecoder.projectQ.decode(
            RelayInboxResponse.self,
            from: data
        )
    }

    private func acknowledge(_ messageID: String) async throws {
        let url = baseURL
            .appendingPathComponent("v1/envelopes")
            .appendingPathComponent(messageID)
            .appendingPathComponent("ack")
        let request = authorizedRequest(url: url, method: "POST")
        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse,
              200..<300 ~= http.statusCode else {
            throw CompanionTransportError.unavailable
        }
    }

    private func authorizedRequest(url: URL, method: String) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(
            "Bearer \(bearerToken)",
            forHTTPHeaderField: "Authorization"
        )
        return request
    }
}
