import Foundation

extension OfflineQueue {
    func flush(using transport: any CompanionTransport) async {
        var remaining: [OfflineCommand] = []
        for var command in allCommands() {
            do {
                _ = try await transport.send(
                    method: command.method,
                    path: command.path,
                    body: command.body,
                    commandID: command.id
                )
            } catch {
                command.attemptCount += 1
                remaining.append(command)
            }
        }
        await replace(with: remaining)
    }
}
