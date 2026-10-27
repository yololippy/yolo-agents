import Foundation

public enum OutgoingStatus: String, Codable, Sendable { case submitting, needsRetry, accepted }

public struct OutgoingMessage: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let conversationID: UUID
    public let content: String
    public let createdAt: Date
    public var status: OutgoingStatus
    public var runID: UUID?
    public var error: String?
}

public struct PartialReply: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let conversationID: UUID
    public var content: String
    public var error: ServiceError?
    public let createdAt: Date
}

/// A page is reduced transactionally. Persist the returned value before publishing it.
/// Cursor gaps are legal (a global log may contain events outside a device's scope),
/// but order, page boundaries, and duplicate events are checked.
public struct ClientState: Codable, Equatable, Sendable {
    public var conversations: [Conversation] = []
    public private(set) var messagesByConversation: [String: [Message]] = [:]
    public private(set) var outgoing: [OutgoingMessage] = []
    public private(set) var partialReplies: [PartialReply] = []
    public private(set) var cursor: Int64 = 0
    public init() {}

    public func messages(in conversationID: UUID) -> [Message] {
        messagesByConversation[conversationID.uuidString] ?? []
    }
    public mutating func upsertConversation(_ conversation: Conversation) {
        if let index = conversations.firstIndex(where: { $0.id == conversation.id }) {
            // A REST snapshot can arrive after a newer event while requests overlap.
            if conversations[index].updatedAt <= conversation.updatedAt { conversations[index] = conversation }
        } else { conversations.append(conversation) }
        conversations.sort { $0.updatedAt == $1.updatedAt ? $0.id.uuidString < $1.id.uuidString : $0.updatedAt > $1.updatedAt }
    }
    public mutating func mergeMessages(_ incoming: [Message], conversationID: UUID) throws {
        guard incoming.allSatisfy({ $0.conversationID == conversationID }) else {
            throw ClientError.invalidEvent("message.conversation_id")
        }
        for message in incoming { upsertMessage(message) }
    }
    private mutating func upsertMessage(_ message: Message) {
        let key = message.conversationID.uuidString
        var messages = messagesByConversation[key] ?? []
        if let index = messages.firstIndex(where: {
            $0.id == message.id || (message.clientMessageID != nil && $0.clientMessageID == message.clientMessageID)
        }) {
            // Never let an older pending snapshot overwrite a terminal server message.
            if messages[index].id != message.id || messages[index].status == .pending ||
                (message.status != .pending && messages[index].status == message.status) {
                messages[index] = message
            }
        } else { messages.append(message) }
        messages.sort { $0.createdAt == $1.createdAt ? $0.id.uuidString < $1.id.uuidString : $0.createdAt < $1.createdAt }
        messagesByConversation[key] = messages
        if message.role == .assistant, message.status != .pending, let runID = message.runID {
            partialReplies.removeAll { $0.id == runID }
            outgoing.removeAll { $0.runID == runID }
        }
    }
    @discardableResult
    public mutating func enqueue(content: String, conversationID: UUID, id: UUID = UUID(), now: Date = Date()) -> UUID {
        guard !outgoing.contains(where: { $0.id == id }) else { return id }
        outgoing.append(OutgoingMessage(id: id, conversationID: conversationID, content: content,
                                        createdAt: now, status: .submitting))
        upsertMessage(Message(id: id, conversationID: conversationID, role: .user, content: content,
                             createdAt: now, status: .pending, clientMessageID: id))
        return id
    }
    public mutating func markSubmitting(_ id: UUID) {
        guard let index = outgoing.firstIndex(where: { $0.id == id }) else { return }
        outgoing[index].status = .submitting; outgoing[index].error = nil
    }
    public mutating func markNeedsRetry(_ id: UUID, error: String) {
        guard let index = outgoing.firstIndex(where: { $0.id == id }) else { return }
        outgoing[index].status = .needsRetry; outgoing[index].error = error
    }
    public mutating func accept(_ response: SendMessageResponse, clientMessageID: UUID) throws {
        guard ["accepted", "duplicate"].contains(response.status),
              response.message.clientMessageID == clientMessageID,
              response.message.role == .user,
              let index = outgoing.firstIndex(where: { $0.id == clientMessageID }),
              outgoing[index].conversationID == response.message.conversationID else {
            throw ClientError.invalidEvent("message.accepted")
        }
        outgoing[index].status = .accepted; outgoing[index].error = nil
        outgoing[index].runID = response.runID
        upsertMessage(response.message)
        // A completion event may beat the POST response.
        if messages(in: response.message.conversationID).contains(where: {
            $0.role == .assistant && $0.runID == response.runID && $0.status != .pending
        }) || partialReplies.contains(where: { $0.id == response.runID && $0.error != nil }) {
            outgoing.removeAll { $0.id == clientMessageID }
        }
    }
    public mutating func prepareForLaunch() {
        for index in outgoing.indices where outgoing[index].status == .submitting {
            outgoing[index].status = .needsRetry
            outgoing[index].error = "上次发送结果尚未确认；重试会使用同一消息 ID。"
        }
    }
    public func applying(_ page: EventsPage) throws -> ClientState {
        var next = self
        var lastSeq = cursor
        var previousSeq: Int64?
        for event in page.events {
            guard event.seq > 0, previousSeq.map({ event.seq > $0 }) ?? true else {
                throw ClientError.invalidEventPage
            }
            previousSeq = event.seq
            if event.seq <= cursor { continue }
            try next.apply(event)
            lastSeq = event.seq
        }
        guard page.nextCursor == lastSeq,
              !(page.hasMore && lastSeq == cursor) else { throw ClientError.invalidEventPage }
        next.cursor = lastSeq
        return next
    }
    public func applying(_ event: ServerEvent) throws -> ClientState {
        try applying(EventsPage(events: [event], nextCursor: max(cursor, event.seq), hasMore: false))
    }
    private mutating func apply(_ event: ServerEvent) throws {
        switch event.type {
        case "conversation.updated":
            guard let conversation = event.payload.conversation,
                  event.conversationID == nil || event.conversationID == conversation.id else {
                throw ClientError.invalidEvent(event.type)
            }
            upsertConversation(conversation)
        case "message.created", "assistant.completed":
            guard let message = event.payload.message,
                  event.conversationID == message.conversationID else { throw ClientError.invalidEvent(event.type) }
            if event.type == "assistant.completed" {
                guard message.role == .assistant, message.status == .completed,
                      let runID = event.payload.runID, message.runID == runID else {
                    throw ClientError.invalidEvent(event.type)
                }
            }
            upsertMessage(message)
        case "assistant.delta", "assistant.failed":
            guard let runID = event.payload.runID, let conversationID = event.conversationID else {
                throw ClientError.invalidEvent(event.type)
            }
            if event.type == "assistant.delta", event.payload.delta == nil { throw ClientError.invalidEvent(event.type) }
            if event.type == "assistant.failed", event.payload.error == nil { throw ClientError.invalidEvent(event.type) }
            if event.type == "assistant.failed", let message = event.payload.message {
                guard message.conversationID == conversationID, message.runID == runID,
                      message.role == .assistant, message.status == .failed else {
                    throw ClientError.invalidEvent(event.type)
                }
                upsertMessage(message)
                return
            }
            if messages(in: conversationID).contains(where: {
                $0.role == .assistant && $0.runID == runID && $0.status != .pending
            }) { return }
            var partial = partialReplies.first(where: { $0.id == runID }) ??
                PartialReply(id: runID, conversationID: conversationID, content: "", createdAt: event.createdAt)
            guard partial.conversationID == conversationID else { throw ClientError.invalidEvent(event.type) }
            if event.type == "assistant.failed" {
                partial.error = event.payload.error
                outgoing.removeAll { $0.runID == runID }
            } else if partial.error == nil { partial.content += event.payload.delta ?? "" }
            partialReplies.removeAll { $0.id == runID }
            partialReplies.append(partial)
        default:
            // Future event types remain forward compatible; they cannot enable actions.
            break
        }
    }
    /// Bounded device-only history. The complete authoritative record stays on the Mac.
    /// Outgoing requests are retained even when their conversation falls outside the window.
    public func cacheSnapshot() -> ClientState {
        var snapshot = self
        let required = Set(outgoing.map(\.conversationID))
        snapshot.conversations = Array(conversations.prefix(50)) +
            conversations.dropFirst(50).filter { required.contains($0.id) }
        let retained = Set(snapshot.conversations.map { $0.id.uuidString })
        snapshot.messagesByConversation = messagesByConversation.filter { retained.contains($0.key) }.mapValues { messages in
            let pendingIDs = Set(outgoing.map(\.id))
            let recent = Array(messages.suffix(300))
            return messages.dropLast(min(300, messages.count)).filter {
                $0.clientMessageID.map { pendingIDs.contains($0) } ?? false
            } + recent
        }
        snapshot.partialReplies = partialReplies.filter { retained.contains($0.conversationID.uuidString) }
        // Keychain is for a small read-only working set. Bound message text as well
        // as message count; retain unsent/unknown intents even under cache pressure.
        let protectedIDs = Set(outgoing.map(\.id))
        let newest = snapshot.messagesByConversation.values.flatMap { $0 }.sorted {
            $0.createdAt == $1.createdAt ? $0.id.uuidString > $1.id.uuidString : $0.createdAt > $1.createdAt
        }
        var budget = 1_500_000
        var kept = Set<UUID>()
        for message in newest {
            let cost = message.content.utf8.count + 512
            let protected = message.clientMessageID.map { protectedIDs.contains($0) } ?? false
            if protected || cost <= budget { kept.insert(message.id); budget -= cost }
        }
        snapshot.messagesByConversation = snapshot.messagesByConversation.mapValues { $0.filter { kept.contains($0.id) } }
        snapshot.partialReplies = snapshot.partialReplies.sorted { $0.createdAt > $1.createdAt }.filter {
            let cost = $0.content.utf8.count + 512
            guard cost <= budget else { return false }
            budget -= cost
            return true
        }
        return snapshot
    }
}
