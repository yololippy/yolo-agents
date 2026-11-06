import Foundation

/// Stable UI identity shared by ordinary conversation rows on both platforms.
enum ConversationRowAccessibility {
    static func identifier(for conversationID: UUID) -> String {
        "chat.conversation-row.\(conversationID.uuidString.lowercased())"
    }
}
