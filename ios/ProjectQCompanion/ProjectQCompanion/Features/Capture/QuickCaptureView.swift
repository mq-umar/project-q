import SwiftUI

struct QuickCaptureView: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @StateObject private var speech = SpeechController()
    @State private var text = ""
    @State private var captureType = "text"

    var body: some View {
        Form {
            Picker("Capture type", selection: $captureType) {
                Label("Text", systemImage: "text.cursor").tag("text")
                Label("Voice", systemImage: "mic").tag("voice")
                Label("URL", systemImage: "link").tag("url")
            }
            .pickerStyle(.segmented)

            if captureType == "url" {
                TextField("https://", text: $text)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
            } else {
                TextEditor(text: $text)
                    .frame(minHeight: 180)
            }
            if captureType == "voice" {
                Button {
                    Task {
                        if speech.isListening {
                            speech.stop()
                        } else {
                            try? await speech.start()
                        }
                    }
                } label: {
                    Label(
                        speech.isListening ? "Stop Recording" : "Record Voice Note",
                        systemImage: speech.isListening ? "stop.fill" : "mic.fill"
                    )
                }
            }
            LabeledContent("Delivery", value: store.transportState.rawValue.capitalized)
            if store.pendingOfflineCount > 0 {
                Text("\(store.pendingOfflineCount) item(s) queued securely.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Button {
                Task {
                    speech.stop()
                    await store.quickCapture(text: text, type: captureType)
                    dismiss()
                }
            } label: {
                Label("Capture", systemImage: "tray.and.arrow.down.fill")
            }
            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .navigationTitle("Quick Capture")
        .onChange(of: speech.transcript) {
            guard captureType == "voice" else { return }
            text = speech.transcript
        }
        .onChange(of: captureType) {
            speech.stop()
            text = ""
        }
    }
}
