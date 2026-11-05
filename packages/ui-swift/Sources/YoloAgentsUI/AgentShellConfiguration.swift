import Foundation
import YoloAgentsSDK

/// Product-neutral labels and conversation grouping for the shared Agent shell.
/// Agent apps inject their domain destinations through ``ChatFeature`` instead
/// of copying the navigation hierarchy.
public struct AgentShellConfiguration: Equatable, Sendable {
    public var conversationsTitle: String
    public var primarySectionTitle: String
    public var recentSectionTitle: String
    public var archivedSectionTitle: String

    public init(
        conversationsTitle: String = "会话",
        primarySectionTitle: String = "主会话",
        recentSectionTitle: String = "最近",
        archivedSectionTitle: String = "已归档"
    ) {
        self.conversationsTitle = conversationsTitle
        self.primarySectionTitle = primarySectionTitle
        self.recentSectionTitle = recentSectionTitle
        self.archivedSectionTitle = archivedSectionTitle
    }

    public static let conversationFirst = AgentShellConfiguration()
}

struct ConversationHomeSections: Equatable {
    let primary: [Conversation]
    let recent: [Conversation]
    let archived: [Conversation]

    init(state: ClientState, includeArchived: Bool) {
        let visible = state.orderedConversations.filter { conversation in
            includeArchived || !conversation.archived
        }
        primary = visible.filter { $0.id == state.primaryConversationID }
        recent = visible.filter { $0.id != state.primaryConversationID && !$0.archived }
        archived = visible.filter { $0.id != state.primaryConversationID && $0.archived }
    }
}
