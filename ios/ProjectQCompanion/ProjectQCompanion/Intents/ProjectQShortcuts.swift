import AppIntents

private func intentStore() async -> CompanionStore {
    let store = await MainActor.run { CompanionStore.live() }
    await store.start()
    return store
}

struct AskProjectQIntent: AppIntent {
    static let title: LocalizedStringResource = "Ask Project Q"
    static let description = IntentDescription(
        "Send a text request to your Windows Project Q runtime."
    )

    @Parameter(title: "Request")
    var request: String

    func perform() async throws -> some IntentResult & ReturnsValue<String> {
        let store = await intentStore()
        await store.sendChat(request)
        let reply = await MainActor.run {
            store.messages.last(where: { $0.role == "assistant" })?.content
        } ?? "Request queued."
        return .result(value: reply)
    }
}

struct QuickCaptureIntent: AppIntent {
    static let title: LocalizedStringResource = "Capture to Project Q"
    static let description = IntentDescription(
        "Securely queue a note for Project Q."
    )
    static let openAppWhenRun = false

    @Parameter(title: "Text")
    var text: String

    func perform() async throws -> some IntentResult {
        let store = await intentStore()
        await store.quickCapture(text: text)
        return .result()
    }
}

struct RunProjectQRoutineIntent: AppIntent {
    static let title: LocalizedStringResource = "Run Project Q Routine"

    @Parameter(title: "Routine ID")
    var routineID: String

    func perform() async throws -> some IntentResult {
        let store = await intentStore()
        let routine = await MainActor.run {
            store.routines.first(where: { $0.id == routineID })
        }
        guard let routine else {
            throw CompanionTransportError.invalidResponse
        }
        await store.runRoutine(routine)
        return .result()
    }
}

struct ProjectQEmergencyStopIntent: AppIntent {
    static let title: LocalizedStringResource = "Stop Project Q"
    static let description = IntentDescription("Activate the Windows kill switch.")
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication

    func perform() async throws -> some IntentResult {
        let store = await intentStore()
        await store.activateKillSwitch()
        return .result()
    }
}

struct ProjectQShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskProjectQIntent(),
            phrases: ["Ask \(.applicationName)"]
        )
        AppShortcut(
            intent: QuickCaptureIntent(),
            phrases: ["Capture to \(.applicationName)"]
        )
        AppShortcut(
            intent: RunProjectQRoutineIntent(),
            phrases: ["Run a routine in \(.applicationName)"]
        )
        AppShortcut(
            intent: ProjectQEmergencyStopIntent(),
            phrases: ["Stop \(.applicationName)"]
        )
    }
}
