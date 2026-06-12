import SwiftUI
import VisionKit

struct PairingView: View {
    @EnvironmentObject private var store: CompanionStore
    @Environment(\.dismiss) private var dismiss
    @State private var payload = ""
    @State private var scanning = false
    @State private var pairing = false

    var body: some View {
        Form {
            Section {
                Button {
                    scanning = true
                } label: {
                    Label("Scan Pairing QR", systemImage: "qrcode.viewfinder")
                }
                TextEditor(text: $payload)
                    .frame(minHeight: 120)
                    .font(.caption.monospaced())
                Button {
                    Task {
                        pairing = true
                        if await store.pair(qrPayload: payload) {
                            dismiss()
                        }
                        pairing = false
                    }
                } label: {
                    Label("Pair", systemImage: "link.badge.plus")
                }
                .disabled(payload.isEmpty || pairing)
            } footer: {
                Text("The pairing payload expires after ten minutes and never contains a shared symmetric key.")
            }
        }
        .navigationTitle("Pair iPhone")
        .sheet(isPresented: $scanning) {
            PairingScanner { value in
                payload = value
                scanning = false
            }
        }
    }
}

private struct PairingScanner: UIViewControllerRepresentable {
    let onScan: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onScan: onScan)
    }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let controller = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isHighlightingEnabled: true
        )
        controller.delegate = context.coordinator
        try? controller.startScanning()
        return controller
    }

    func updateUIViewController(
        _ uiViewController: DataScannerViewController,
        context: Context
    ) {}

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let onScan: (String) -> Void

        init(onScan: @escaping (String) -> Void) {
            self.onScan = onScan
        }

        func dataScanner(
            _ dataScanner: DataScannerViewController,
            didAdd addedItems: [RecognizedItem],
            allItems: [RecognizedItem]
        ) {
            for item in addedItems {
                guard case .barcode(let barcode) = item,
                      let payload = barcode.payloadStringValue else {
                    continue
                }
                dataScanner.stopScanning()
                onScan(payload)
                return
            }
        }
    }
}
