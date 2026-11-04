import Foundation

public enum OutgoingStatus: String, Codable, Sendable { case submitting, needsRetry, accepted }

public struct OutgoingMessage: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let conversationID: UUID
    public let content: String
    public let mediaIDs: [UUID]
    public let createdAt: Date
    public var status: OutgoingStatus
    public var runID: UUID?
    public var error: String?

    enum CodingKeys: String, CodingKey {
        case id, conversationID, content, mediaIDs, createdAt, status, runID, error
    }

    public init(id: UUID, conversationID: UUID, content: String, mediaIDs: [UUID] = [],
                createdAt: Date, status: OutgoingStatus, runID: UUID? = nil, error: String? = nil) {
        self.id = id; self.conversationID = conversationID; self.content = content
        self.mediaIDs = mediaIDs; self.createdAt = createdAt; self.status = status
        self.runID = runID; self.error = error
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UUID.self, forKey: .id)
        conversationID = try values.decode(UUID.self, forKey: .conversationID)
        content = try values.decode(String.self, forKey: .content)
        mediaIDs = try values.decodeIfPresent([UUID].self, forKey: .mediaIDs) ?? []
        createdAt = try values.decode(Date.self, forKey: .createdAt)
        status = try values.decode(OutgoingStatus.self, forKey: .status)
        runID = try values.decodeIfPresent(UUID.self, forKey: .runID)
        error = try values.decodeIfPresent(String.self, forKey: .error)
    }
}

public struct PartialReply: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let conversationID: UUID
    public var content: String
    public var media: [MessageMedia]
    public var error: ServiceError?
    public let createdAt: Date

    enum CodingKeys: String, CodingKey { case id, conversationID, content, media, error, createdAt }

    public init(id: UUID, conversationID: UUID, content: String,
                media: [MessageMedia] = [], error: ServiceError? = nil, createdAt: Date) {
        self.id = id; self.conversationID = conversationID; self.content = content
        self.media = media; self.error = error; self.createdAt = createdAt
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UUID.self, forKey: .id)
        conversationID = try values.decode(UUID.self, forKey: .conversationID)
        content = try values.decode(String.self, forKey: .content)
        media = try values.decodeIfPresent([MessageMedia].self, forKey: .media) ?? []
        error = try values.decodeIfPresent(ServiceError.self, forKey: .error)
        createdAt = try values.decode(Date.self, forKey: .createdAt)
    }
}

public struct AwaitingReply: Equatable, Sendable, Identifiable {
    public let id: UUID
    public let conversationID: UUID
    public let createdAt: Date
    public init(id: UUID, conversationID: UUID, createdAt: Date) {
        self.id = id; self.conversationID = conversationID; self.createdAt = createdAt
    }
}

/// A page is reduced transactionally. Persist the returned value before publishing it.
/// Cursor gaps are legal (a global log may contain events outside a device's scope),
/// but order, page boundaries, and duplicate events are checked.
public struct ClientState: Codable, Equatable, Sendable {
    public var conversations: [Conversation] = []
    public var primaryConversationID: UUID?
    public private(set) var messagesByConversation: [String: [Message]] = [:]
    public private(set) var outgoing: [OutgoingMessage] = []
    public private(set) var partialReplies: [PartialReply] = []
    public private(set) var cursor: Int64 = 0
    public init() {}

    /// Primary is a stable product-level conversation, so it stays above newer
    /// task conversations on every client. The remaining conversations retain
    /// the existing most-recently-updated ordering.
    public var orderedConversations: [Conversation] {
        conversations.sorted { lhs, rhs in
            let lhsIsPrimary = lhs.id == primaryConversationID
            let rhsIsPrimary = rhs.id == primaryConversationID
            if lhsIsPrimary != rhsIsPrimary { return lhsIsPrimary }
            return lhs.updatedAt == rhs.updatedAt
                ? lhs.id.uuidString < rhs.id.uuidString
                : lhs.updatedAt > rhs.updatedAt
        }
    }

    public mutating func setPrimaryConversation(_ id: UUID?) {
        primaryConversationID = id
        conversations = orderedConversations
    }

    public func messages(in conversationID: UUID) -> [Message] {
        messagesByConversation[conversationID.uuidString] ?? []
    }
    /// Accepted runs that have not produced their first assistant event yet.
    /// This is derived so terminal events arriving before the POST response cannot
    /// briefly resurrect a waiting indicator.
    public func awaitingReplies(in conversationID: UUID) -> [AwaitingReply] {
        let partialRunIDs = Set(partialReplies.lazy.filter {
            $0.conversationID == conversationID
        }.map(\.id))
        let terminalRunIDs = Set(messages(in: conversationID).lazy.compactMap { message in
            message.role == .assistant && message.status != .pending ? message.runID : nil
        })
        var seen = Set<UUID>()
        return outgoing.lazy
            .filter { $0.conversationID == conversationID && $0.status == .accepted }
            .compactMap { outgoing -> AwaitingReply? in
                guard let runID = outgoing.runID, seen.insert(runID).inserted,
                      !partialRunIDs.contains(runID), !terminalRunIDs.contains(runID) else { return nil }
                return AwaitingReply(id: runID, conversationID: conversationID, createdAt: outgoing.createdAt)
            }
            .sorted { $0.createdAt == $1.createdAt ? $0.id.uuidString < $1.id.uuidString : $0.createdAt < $1.createdAt }
    }
    public mutating func upsertConversation(_ conversation: Conversation) {
        if let index = conversations.firstIndex(where: { $0.id == conversation.id }) {
            // A REST snapshot can arrive after a newer event while requests overlap.
            if conversations[index].updatedAt <= conversation.updatedAt { conversations[index] = conversation }
        } else { conversations.append(conversation) }
        conversations = orderedConversations
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
    public mutating func enqueue(content: String, conversationID: UUID, media: [MessageMedia] = [],
                                 id: UUID = UUID(), now: Date = Date()) -> UUID {
        guard !outgoing.contains(where: { $0.id == id }) else { return id }
        outgoing.append(OutgoingMessage(id: id, conversationID: conversationID, content: content,
                                        mediaIDs: media.map(\.id),
                                        createdAt: now, status: .submitting))
        upsertMessage(Message(id: id, conversationID: conversationID, role: .user, content: content,
                             createdAt: now, status: .pending, clientMessageID: id, media: media))
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
              outgoing[index].conversationID == response.message.conversationID,
              outgoing[index].mediaIDs == response.message.media.map(\.id) else {
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
        case "assistant.delta", "assistant.media", "assistant.failed":
            guard let runID = event.payload.runID, let conversationID = event.conversationID else {
                throw ClientError.invalidEvent(event.type)
            }
            if event.type == "assistant.delta", event.payload.delta == nil { throw ClientError.invalidEvent(event.type) }
            if event.type == "assistant.media", event.payload.media == nil { throw ClientError.invalidEvent(event.type) }
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
            } else if event.type == "assistant.media", partial.error == nil, let media = event.payload.media {
                if !partial.media.contains(where: { $0.id == media.id }) { partial.media.append(media) }
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
        let ordered = orderedConversations
        snapshot.conversations = Array(ordered.prefix(50)) +
            ordered.dropFirst(50).filter { required.contains($0.id) }
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
