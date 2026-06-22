import AVFoundation
import Foundation
import Speech

@MainActor
final class SpeechController: ObservableObject {
    @Published var transcript = ""
    @Published var isListening = false

    private let recognizer = SFSpeechRecognizer()
    private let audioEngine = AVAudioEngine()
    private let synthesizer = AVSpeechSynthesizer()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var tapInstalled = false

    func start() async throws {
        synthesizer.stopSpeaking(at: .immediate)
        let speechStatus = await SFSpeechRecognizer.requestAuthorization()
        guard speechStatus == .authorized else {
            throw CompanionTransportError.unavailable
        }
        request = SFSpeechAudioBufferRecognitionRequest()
        guard let request else { throw CompanionTransportError.unavailable }
        request.shouldReportPartialResults = true
        let node = audioEngine.inputNode
        let format = node.outputFormat(forBus: 0)
        if tapInstalled {
            node.removeTap(onBus: 0)
            tapInstalled = false
        }
        node.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            request.append(buffer)
        }
        tapInstalled = true
        audioEngine.prepare()
        do {
            try audioEngine.start()
        } catch {
            node.removeTap(onBus: 0)
            tapInstalled = false
            self.request = nil
            throw error
        }
        isListening = true
        task = recognizer?.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in
                if let result {
                    self?.transcript = result.bestTranscription.formattedString
                }
                if error != nil || result?.isFinal == true {
                    self?.stop()
                }
            }
        }
    }

    func stop() {
        audioEngine.stop()
        if tapInstalled {
            audioEngine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
        isListening = false
    }

    func speak(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        synthesizer.speak(utterance)
    }
}

private extension SFSpeechRecognizer {
    static func requestAuthorization() async -> SFSpeechRecognizerAuthorizationStatus {
        await withCheckedContinuation { continuation in
            requestAuthorization { continuation.resume(returning: $0) }
        }
    }
}
