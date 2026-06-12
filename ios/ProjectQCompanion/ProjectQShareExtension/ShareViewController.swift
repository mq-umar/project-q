import Social
import UniformTypeIdentifiers

@objc(ShareViewController)
final class ShareViewController: SLComposeServiceViewController {
    override func isContentValid() -> Bool {
        !(contentText ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || !(extensionContext?.inputItems ?? []).isEmpty
    }

    override func didSelectPost() {
        Task {
            let text = await sharedText()
            let queue = OfflineQueue()
            await queue.load()
            let body = try? JSONEncoder.projectQ.encode([
                "text": JSONValue.string(text),
                "capture_type": .string(text.hasPrefix("http") ? "url" : "text"),
                "metadata": .object(["source": .string("share_extension")]),
            ])
            await queue.enqueue(
                method: "POST",
                path: "/api/companion/v2/quick-capture",
                body: body
            )
            extensionContext?.completeRequest(returningItems: nil)
        }
    }

    override func configurationItems() -> [Any]! {
        []
    }

    private func sharedText() async -> String {
        if let contentText, !contentText.isEmpty {
            return contentText
        }
        for case let item as NSExtensionItem in extensionContext?.inputItems ?? [] {
            for provider in item.attachments ?? [] {
                if provider.hasItemConformingToTypeIdentifier(UTType.url.identifier),
                   let item = try? await provider.loadItem(
                    forTypeIdentifier: UTType.url.identifier
                   ),
                   let url = item as? URL {
                    return url.absoluteString
                }
                if provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier),
                   let item = try? await provider.loadItem(
                    forTypeIdentifier: UTType.plainText.identifier
                   ),
                   let text = item as? String {
                    return text
                }
            }
        }
        return ""
    }
}
