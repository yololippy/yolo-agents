import Foundation
import Testing
@testable import YoloAgentsUI

@Test func conversationRowIdentifierUsesCanonicalLowercaseUUID() throws {
    let uppercase = try #require(UUID(uuidString: "ABCDEF12-3456-4789-ABCD-EF1234567890"))
    let lowercase = try #require(UUID(uuidString: "abcdef12-3456-4789-abcd-ef1234567890"))

    #expect(ConversationRowAccessibility.identifier(for: uppercase) ==
            "chat.conversation-row.abcdef12-3456-4789-abcd-ef1234567890")
    #expect(ConversationRowAccessibility.identifier(for: lowercase) ==
            "chat.conversation-row.abcdef12-3456-4789-abcd-ef1234567890")
}

@Test func differentConversationsHaveDistinctRowIdentifiers() throws {
    let first = try #require(UUID(uuidString: "ABCDEF12-3456-4789-ABCD-EF1234567890"))
    let second = try #require(UUID(uuidString: "ABCDEF12-3456-4789-ABCD-EF1234567891"))
    let firstIdentifier = ConversationRowAccessibility.identifier(for: first)
    let secondIdentifier = ConversationRowAccessibility.identifier(for: second)

    #expect(firstIdentifier == "chat.conversation-row.abcdef12-3456-4789-abcd-ef1234567890")
    #expect(secondIdentifier == "chat.conversation-row.abcdef12-3456-4789-abcd-ef1234567891")
    #expect(firstIdentifier != secondIdentifier)
}
