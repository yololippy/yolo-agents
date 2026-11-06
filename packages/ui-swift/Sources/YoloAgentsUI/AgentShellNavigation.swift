import Foundation

/// Typed destinations keep app-owned feature identifiers separate from shell destinations.
enum AgentShellDestination: Hashable, Sendable {
    case conversations
    case feature(String)
    case account
    case settings
}

/// The iPhone shell owns the conversation path independently of the active page.
/// Returning from another page restores the detail that was open before switching.
struct AgentShellNavigation: Equatable, Sendable {
    private(set) var destination: AgentShellDestination = .conversations
    var conversationPath: [UUID] = []
    var drafts = ComposerDrafts()

    @MainActor
    static func dockItems(features: [ChatFeature], configuration: AgentShellConfiguration)
        -> [AgentActionDockItem<AgentShellDestination>] {
        [AgentActionDockItem(
            id: .conversations,
            title: configuration.conversationsTitle,
            systemImage: "bubble.left.and.bubble.right",
            accessibilityIdentifier: "chat.conversations"
        )] + features.map {
            AgentActionDockItem(
                id: .feature($0.id),
                title: $0.title,
                compactTitle: $0.compactTitle,
                systemImage: $0.systemImage,
                accessibilityIdentifier: $0.id
            )
        } + [
            AgentActionDockItem(
                id: .account,
                title: "ChatGPT 账号",
                compactTitle: "账号",
                systemImage: "person.crop.circle",
                accessibilityIdentifier: "chat.account"
            ),
            AgentActionDockItem(
                id: .settings,
                title: "连接设置",
                compactTitle: "设置",
                systemImage: "gearshape",
                accessibilityIdentifier: "chat.settings"
            ),
        ]
    }

    @discardableResult
    mutating func select(_ destination: AgentShellDestination, availableFeatureIDs: [String]) -> Bool {
        if case .feature(let id) = destination, !availableFeatureIDs.contains(id) { return false }
        self.destination = destination
        return true
    }
}

/// Pages have their own navigation container. Only modal presentations offer a
/// dismissal action; the default preserves existing sheet callers.
public enum AgentContentPresentation: Equatable, Sendable {
    case modal
    case page

    public var showsDismissAction: Bool { self == .modal }
}
