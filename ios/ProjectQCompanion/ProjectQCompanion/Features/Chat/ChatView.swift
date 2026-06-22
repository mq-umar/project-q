import SwiftUI

struct ChatView: View {
    @EnvironmentObject private var store: CompanionStore
    @StateObject private var model = ChatViewModel()

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    List(store.messages) { message in
                        HStack {
                            if message.role == "assistant" {
                                Text(message.content)
                                    .textSelection(.enabled)
                                Spacer(minLength: 36)
                            } else {
                                Spacer(minLength: 36)
                                Text(message.content)
                                    .textSelection(.enabled)
                                    .foregroundStyle(.white)
                                    .padding(10)
                                    .background(Color.accentColor, in: RoundedRectangle(cornerRadius: 8))
                            }
                        }
                        .id(message.id)
                    }
                    .listStyle(.plain)
                    .onChange(of: store.messages.count) {
                        if let id = store.messages.last?.id {
                            proxy.scrollTo(id, anchor: .bottom)
                        }
                    }
                }

                HStack(spacing: 10) {
                    TextField("Message Project Q", text: $model.draft, axis: .vertical)
                        .textFieldStyle(.roundedBorder)
                        .lineLimit(1...5)
                    Button {
                        Task {
                            if model.speech.isListening {
                                model.speech.stop()
                                model.draft = model.speech.transcript
                            } else {
                                try? await model.speech.start()
                            }
                        }
                    } label: {
                        Image(systemName: model.speech.isListening ? "stop.fill" : "mic.fill")
                    }
                    .buttonStyle(.bordered)
                    .accessibilityLabel(model.speech.isListening ? "Stop listening" : "Start listening")
                    Button {
                        Task { await model.send(using: store) }
                    } label: {
                        Image(systemName: "arrow.up.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("Send")
                }
                .padding()
            }
            .navigationTitle("Chat")
        }
    }
}
