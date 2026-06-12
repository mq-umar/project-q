import Foundation

enum CompanionTransportError: Error {
    case unavailable
    case invalidResponse
    case server(Int, String)
    case queuedOffline
}

struct CompanionTransportResult: Sendable {
    let data: Data
    let state: CompanionTransportState
    let queued: Bool
}

protocol CompanionTransport: Sendable {
    func send(
        method: String,
        path: String,
        body: Data?,
        commandID: UUID
    ) async throws -> CompanionTransportResult

    func streamChat(
        body: Data,
        commandID: UUID
    ) async -> AsyncThrowingStream<CompanionChatStreamEvent, Error>
}

extension CompanionTransport {
    func send(
        method: String,
        path: String,
        body: Data?
    ) async throws -> CompanionTransportResult {
        try await send(
            method: method,
            path: path,
            body: body,
            commandID: UUID()
        )
    }

    func streamChat(
        body: Data,
        commandID: UUID = UUID()
    ) async -> AsyncThrowingStream<CompanionChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let result = try await send(
                        method: "POST",
                        path: "/api/companion/v2/chat",
                        body: body,
                        commandID: commandID
                    )
                    let response = try JSONDecoder.projectQ.decode(
                        CompanionChatResponse.self,
                        from: result.data
                    )
                    continuation.yield(
                        CompanionChatStreamEvent(
                            token: response.reply,
                            response: nil,
                            state: result.state
                        )
                    )
                    continuation.yield(
                        CompanionChatStreamEvent(
                            token: "",
                            response: response,
                            state: result.state
                        )
                    )
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

actor TransportCoordinator: CompanionTransport {
    private let direct: DirectTransport
    private let relay: RelayTransport?
    private let queue: OfflineQueue

    init(
        direct: DirectTransport,
        relay: RelayTransport?,
        queue: OfflineQueue
    ) {
        self.direct = direct
        self.relay = relay
        self.queue = queue
    }

    func send(
        method: String,
        path: String,
        body: Data?,
        commandID: UUID
    ) async throws -> CompanionTransportResult {
        do {
            return try await direct.send(
                method: method,
                path: path,
                body: body,
                commandID: commandID
            )
        } catch {
            if let relay {
                do {
                    return try await relay.send(
                        method: method,
                        path: path,
                        body: body,
                        commandID: commandID
                    )
                } catch {
                    await queue.enqueue(
                        commandID: commandID,
                        method: method,
                        path: path,
                        body: body
                    )
                    throw CompanionTransportError.queuedOffline
                }
            }
            await queue.enqueue(
                commandID: commandID,
                method: method,
                path: path,
                body: body
            )
            throw CompanionTransportError.queuedOffline
        }
    }

    func streamChat(
        body: Data,
        commandID: UUID
    ) async -> AsyncThrowingStream<CompanionChatStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                var emittedToken = false
                do {
                    let directStream = await direct.streamChat(
                        body: body,
                        commandID: commandID
                    )
                    for try await event in directStream {
                        emittedToken = emittedToken || !event.token.isEmpty
                        continuation.yield(event)
                    }
                    continuation.finish()
                    return
                } catch where emittedToken {
                    continuation.finish(throwing: error)
                    return
                } catch {
                    guard let relay else {
                        await queue.enqueue(
                            commandID: commandID,
                            method: "POST",
                            path: "/api/companion/v2/chat",
                            body: body
                        )
                        continuation.finish(
                            throwing: CompanionTransportError.queuedOffline
                        )
                        return
                    }
                    do {
                        let result = try await relay.send(
                            method: "POST",
                            path: "/api/companion/v2/chat",
                            body: body,
                            commandID: commandID
                        )
                        let response = try JSONDecoder.projectQ.decode(
                            CompanionChatResponse.self,
                            from: result.data
                        )
                        continuation.yield(
                            CompanionChatStreamEvent(
                                token: response.reply,
                                response: nil,
                                state: .relay
                            )
                        )
                        continuation.yield(
                            CompanionChatStreamEvent(
                                token: "",
                                response: response,
                                state: .relay
                            )
                        )
                        continuation.finish()
                    } catch {
                        await queue.enqueue(
                            commandID: commandID,
                            method: "POST",
                            path: "/api/companion/v2/chat",
                            body: body
                        )
                        continuation.finish(
                            throwing: CompanionTransportError.queuedOffline
                        )
                    }
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
