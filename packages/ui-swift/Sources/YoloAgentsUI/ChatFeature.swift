import SwiftUI
import YoloAgentsSDK

/// An app-owned destination. Shared chat UI supplies only navigation and the paired connection.
@MainActor
public struct ChatFeature: Identifiable {
    public let id: String
    public let title: String
    public let systemImage: String
    let content: @MainActor (PairedConnection) -> AnyView

    public init<Content: View>(id: String, title: String, systemImage: String,
                              @ViewBuilder content: @escaping @MainActor (PairedConnection) -> Content) {
        self.id = id; self.title = title; self.systemImage = systemImage
        self.content = { AnyView(content($0)) }
    }
}
