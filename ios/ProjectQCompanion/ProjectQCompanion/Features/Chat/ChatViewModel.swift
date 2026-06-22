import Foundation

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var draft = ""
    @Published var isSending = false
    let speech = SpeechController()

    func send(using store: CompanionStore) async {
        let message = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { return }
        draft = ""
        isSending = true
        await store.sendChat(message)
        isSending = false
        if let reply = store.messages.last, reply.role == "assistant" {
            speech.speak(reply.content)
        }
    }
}
