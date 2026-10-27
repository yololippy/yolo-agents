import Foundation

public struct ComposerAvailability: Equatable, Sendable {
    public let canEdit: Bool
    public let canSend: Bool
    public init(hasConversation: Bool, archived: Bool, online: Bool, busy: Bool) {
        canEdit = hasConversation && !archived
        canSend = canEdit && online && !busy
    }
}

public struct DraftSubmission: Equatable, Sendable {
    public let conversationID: UUID
    public let content: String
}

/// Drafts belong to conversations. A late send response must not clear text edited
/// while the request was in flight, or the draft in another conversation.
public struct ComposerDrafts: Equatable, Sendable {
    private var values: [UUID: String] = [:]
    public init() {}
    public func text(for id: UUID) -> String { values[id] ?? "" }
    public mutating func set(_ text: String, for id: UUID) { values[id] = text.isEmpty ? nil : text }
    public func submission(for id: UUID) -> DraftSubmission? {
        let content = text(for: id)
        guard !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return DraftSubmission(conversationID: id, content: content)
    }
    public mutating func finish(_ submission: DraftSubmission, accepted: Bool) {
        if accepted && text(for: submission.conversationID) == submission.content {
            values[submission.conversationID] = nil
        }
    }
}
