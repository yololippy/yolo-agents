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
    public let images: [DraftImage]
}

/// Drafts belong to conversations. A late send response must not clear text edited
/// while the request was in flight, or the draft in another conversation.
public struct ComposerDrafts: Equatable, Sendable {
    private struct Value: Equatable, Sendable {
        var text = ""
        var images: [DraftImage] = []
    }
    private var values: [UUID: Value] = [:]
    public init() {}
    public func text(for id: UUID) -> String { values[id]?.text ?? "" }
    public func images(for id: UUID) -> [DraftImage] { values[id]?.images ?? [] }
    public mutating func set(_ text: String, for id: UUID) {
        var value = values[id] ?? Value()
        value.text = text
        values[id] = value.text.isEmpty && value.images.isEmpty ? nil : value
    }
    public mutating func setImages(_ images: [DraftImage], for id: UUID) {
        var value = values[id] ?? Value()
        value.images = images
        values[id] = value.text.isEmpty && value.images.isEmpty ? nil : value
    }
    public func submission(for id: UUID) -> DraftSubmission? {
        let content = text(for: id)
        let images = images(for: id)
        guard !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !images.isEmpty else { return nil }
        return DraftSubmission(conversationID: id, content: content, images: images)
    }
    public mutating func finish(_ submission: DraftSubmission, accepted: Bool) {
        if accepted && text(for: submission.conversationID) == submission.content &&
            images(for: submission.conversationID) == submission.images {
            values[submission.conversationID] = nil
        }
    }
}
