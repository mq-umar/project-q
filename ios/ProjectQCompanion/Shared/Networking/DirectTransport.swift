import Foundation

private struct CompanionSSEPayload: Decodable {
    let token: String?
    let done: Bool?
    let reply: String?
    let createdActionRequestIDs: [String]?
    let error: String?
}

struct DirectTransport: CompanionTransport {
    let baseURL: URL
    let signer: AuthenticatedRequestSigner
    var session: URLSession = .shared

    func send(
        method: String,
        path: String,
        body: Data?,
        commandID: UUID
    ) async throws -> CompanionTransportResult {
        _ = commandID
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw CompanionTransportError.unavailable
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 20
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let requestTarget = url.path + (url.query.map { "?\($0)" } ?? "")
        let headers = try await signer.sign(
            method: method,
            requestTarget: requestTarget,
            body: body ?? Data()
        )
        for (name, value) in headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CompanionTransportError.invalidResponse
        }
        guard 200..<300 ~= http.statusCode else {
            throw CompanionTransportError.server(
                http.statusCode,
                String(data: data, encoding: .utf8) ?? ""
            )
        }
        return CompanionTransportResult(data: data, state: .direct, queued: false)
    }

    func streamChat(
        body: Data,
        commandID: UUID
    ) async -> AsyncThrowingStream<CompanionChatStreamEvent, Error> {
        _ = commandID
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    guard let url = URL(
                        string: "/api/companion/v2/chat/stream",
                        relativeTo: baseURL
                    )?.absoluteURL else {
                        throw CompanionTransportError.unavailable
                    }
                    var request = URLRequest(url: url)
                    request.httpMethod = "POST"
                    request.timeoutInterval = 120
                    request.httpBody = body
                    request.setValue(
                        "application/json",
                        forHTTPHeaderField: "Content-Type"
                    )
                    request.setValue(
                        "text/event-stream",
                        forHTTPHeaderField: "Accept"
                    )
                    let headers = try await signer.sign(
                        method: "POST",
                        requestTarget: url.path,
                        body: body
                    )
                    for (name, value) in headers {
                        request.setValue(value, forHTTPHeaderField: name)
                    }

                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw CompanionTransportError.invalidResponse
                    }
                    guard 200..<300 ~= http.statusCode else {
                        throw CompanionTransportError.server(
                            http.statusCode,
                            "companion stream request failed"
                        )
                    }

                    var accumulatedReply = ""
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data: ") else { continue }
                        let payloadData = Data(line.dropFirst(6).utf8)
                        let payload = try JSONDecoder.projectQ.decode(
                            CompanionSSEPayload.self,
                            from: payloadData
                        )
                        if let error = payload.error, !error.isEmpty {
                            throw CompanionTransportError.server(500, error)
                        }
                        if let token = payload.token, !token.isEmpty {
                            accumulatedReply += token
                            continuation.yield(
                                CompanionChatStreamEvent(
                                    token: token,
                                    response: nil,
                                    state: .direct
                                )
                            )
                        }
                        if payload.done == true {
                            continuation.yield(
                                CompanionChatStreamEvent(
                                    token: "",
                                    response: CompanionChatResponse(
                                        reply: payload.reply ?? accumulatedReply,
                                        createdActionRequestIDs:
                                            payload.createdActionRequestIDs ?? []
                                    ),
                                    state: .direct
                                )
                            )
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
