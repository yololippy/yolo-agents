import Foundation
import YoloAgentsSDK

enum ConversationScrollTarget: Hashable {
    case message(UUID)
    case partialReply(UUID)
    case awaitingReply(UUID)
    case bottom(UUID)
}

/// Keeps a conversation pinned to its latest content until the user deliberately
/// scrolls to an older target. Content updates never opt the user back into following.
struct ConversationScrollState: Equatable {
    private(set) var conversationID: UUID?
    private(set) var target: ConversationScrollTarget?
    private(set) var followsLatest = false

    mutating func open(_ conversationID: UUID) {
        self.conversationID = conversationID
        target = .bottom(conversationID)
        followsLatest = true
    }

    mutating func positionChanged(_ target: ConversationScrollTarget?, in conversationID: UUID) {
        guard self.conversationID == conversationID else { return }
        self.target = target
        if let target { followsLatest = target == .bottom(conversationID) }
    }

    mutating func contentChanged(in conversationID: UUID) {
        guard self.conversationID == conversationID, followsLatest else { return }
        target = .bottom(conversationID)
    }
}

/// Presentation-only activity derived from the durable per-conversation client state.
/// No Agent-specific capability or private business state is required.
struct ConversationActivity: Equatable {
    let showsProgress: Bool

    init(state: ClientState, conversationID: UUID) {
        showsProgress = state.isRunning(conversationID: conversationID)
    }
}
