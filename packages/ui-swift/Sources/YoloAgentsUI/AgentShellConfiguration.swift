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
    public var searchPrompt: String

    public init(
        conversationsTitle: String = "会话",
        primarySectionTitle: String = "主会话",
        recentSectionTitle: String = "最近",
        archivedSectionTitle: String = "已归档",
        searchPrompt: String = "搜索会话"
    ) {
        self.conversationsTitle = conversationsTitle
        self.primarySectionTitle = primarySectionTitle
        self.recentSectionTitle = recentSectionTitle
        self.archivedSectionTitle = archivedSectionTitle
        self.searchPrompt = searchPrompt
    }

    public static let conversationFirst = AgentShellConfiguration()
}

struct ConversationHomeSections: Equatable {
    let primary: [Conversation]
    let recent: [Conversation]
    let archived: [Conversation]

    init(state: ClientState, query: String, includeArchived: Bool) {
        let normalizedQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let visible = state.orderedConversations.filter { conversation in
            (includeArchived || !conversation.archived) &&
                (normalizedQuery.isEmpty ||
                    conversation.title.localizedCaseInsensitiveContains(normalizedQuery))
        }
        primary = visible.filter { $0.id == state.primaryConversationID }
        recent = visible.filter { $0.id != state.primaryConversationID && !$0.archived }
        archived = visible.filter { $0.id != state.primaryConversationID && $0.archived }
    }
}
