import Foundation

public enum DevicePlatform: String, Codable, Sendable { case ios, macos }
public enum MessageRole: String, Codable, Sendable { case user, assistant, system }
public enum MessageStatus: String, Codable, Sendable { case pending, completed, failed }

public enum MediaLimits {
    public static let maximumItemsPerMessage = 4
    public static let maximumBytesPerItem = 10 * 1024 * 1024
    public static let maximumBytesPerMessage = 20 * 1024 * 1024
    public static let maximumDimension = 8_192
    public static let maximumPixelCount = 25_000_000
    public static let allowedImageTypes: Set<String> = ["image/jpeg", "image/png", "image/webp"]
}

/// Server-owned metadata for an authenticated media object. The service never
/// exposes its local file path; clients retrieve the bytes through `/v1/media/{id}`.
public struct MessageMedia: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let kind: String
    public let mediaType: String
    public let byteCount: Int
    public let sha256: String
    public let width: Int?
    public let height: Int?
    public let filename: String?
    public let altText: String?
    public let createdAt: Date

    public init(id: UUID, kind: String = "image", mediaType: String, byteCount: Int,
                sha256: String, width: Int? = nil, height: Int? = nil,
                filename: String? = nil, altText: String? = nil, createdAt: Date) {
        self.id = id; self.kind = kind; self.mediaType = mediaType
        self.byteCount = byteCount; self.sha256 = sha256
        self.width = width; self.height = height; self.filename = filename
        self.altText = altText; self.createdAt = createdAt
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, sha256, width, height, filename
        case mediaType = "media_type", byteCount = "byte_count", createdAt = "created_at"
        case altText = "alt_text"
    }
}

public struct Device: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let name: String
    public let platform: DevicePlatform
    public let permissions: [String]
    public let createdAt: Date
    public let revokedAt: Date?
    enum CodingKeys: String, CodingKey {
        case id, name, platform, permissions
        case createdAt = "created_at", revokedAt = "revoked_at"
    }
}

public struct PairingResponse: Codable, Equatable, Sendable {
    public let device: Device
    public let token: String
    public let serverID: UUID
    public let apiVersion: Int
    enum CodingKeys: String, CodingKey {
        case device, token
        case serverID = "server_id", apiVersion = "api_version"
    }
}

/// A provider or integration advertised by the Agent host. Unknown component
/// IDs and states remain display-only so future Agents can add their own.
public struct ServiceComponentHealth: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let label: String
    public let state: String
    public let detail: String?

    public init(id: String, label: String, state: String, detail: String? = nil) {
        self.id = id; self.label = label; self.state = state; self.detail = detail
    }
}

public struct Health: Codable, Equatable, Sendable {
    public let status: String
    public let apiVersion: Int
    public let serverID: UUID
    public let agentMode: String
    public let model: String?
    public let tradeExecutionEnabled: Bool
    public let agentID: String?
    public let appName: String?
    public let components: [ServiceComponentHealth]
    enum CodingKeys: String, CodingKey {
        case status, model, components
        case apiVersion = "api_version", serverID = "server_id"
        case agentMode = "agent_mode", tradeExecutionEnabled = "trade_execution_enabled"
        case agentID = "agent_id", appName = "app_name"
    }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        status = try values.decode(String.self, forKey: .status)
        apiVersion = try values.decode(Int.self, forKey: .apiVersion)
        serverID = try values.decode(UUID.self, forKey: .serverID)
        agentMode = try values.decode(String.self, forKey: .agentMode)
        model = try values.decodeIfPresent(String.self, forKey: .model)
        // Non-trading Agents do not advertise this capability. Absence grants no execution.
        tradeExecutionEnabled = try values.decodeIfPresent(Bool.self, forKey: .tradeExecutionEnabled) ?? false
        agentID = try values.decodeIfPresent(String.self, forKey: .agentID)
        appName = try values.decodeIfPresent(String.self, forKey: .appName)
        components = try values.decodeIfPresent([ServiceComponentHealth].self, forKey: .components) ?? []
    }
}

public struct Conversation: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public var title: String
    public let createdAt: Date
    public var updatedAt: Date
    public var archived: Bool
    public var model: String?
    public init(id: UUID, title: String, createdAt: Date, updatedAt: Date, archived: Bool = false, model: String? = nil) {
        self.id = id; self.title = title; self.createdAt = createdAt
        self.updatedAt = updatedAt; self.archived = archived
        self.model = model
    }
    enum CodingKeys: String, CodingKey {
        case id, title, archived, model
        case createdAt = "created_at", updatedAt = "updated_at"
    }
}

public struct ConversationList: Codable, Equatable, Sendable {
    public let conversations: [Conversation]
    /// Absent only when talking to a pre-primary-conversation v1 service.
    public let primaryConversationID: UUID?
    public init(conversations: [Conversation], primaryConversationID: UUID?) {
        self.conversations = conversations
        self.primaryConversationID = primaryConversationID
    }
    enum CodingKeys: String, CodingKey {
        case conversations
        case primaryConversationID = "primary_conversation_id"
    }
}

public struct Message: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let conversationID: UUID
    public let role: MessageRole
    public let content: String
    public let createdAt: Date
    public var status: MessageStatus
    public let clientMessageID: UUID?
    public let runID: UUID?
    public let media: [MessageMedia]
    public init(id: UUID, conversationID: UUID, role: MessageRole, content: String,
                createdAt: Date, status: MessageStatus, clientMessageID: UUID? = nil,
                runID: UUID? = nil, media: [MessageMedia] = []) {
        self.id = id; self.conversationID = conversationID; self.role = role
        self.content = content; self.createdAt = createdAt; self.status = status
        self.clientMessageID = clientMessageID; self.runID = runID; self.media = media
    }
    enum CodingKeys: String, CodingKey {
        case id, role, content, status, media
        case conversationID = "conversation_id", createdAt = "created_at"
        case clientMessageID = "client_message_id", runID = "run_id"
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UUID.self, forKey: .id)
        conversationID = try values.decode(UUID.self, forKey: .conversationID)
        role = try values.decode(MessageRole.self, forKey: .role)
        content = try values.decode(String.self, forKey: .content)
        createdAt = try values.decode(Date.self, forKey: .createdAt)
        status = try values.decode(MessageStatus.self, forKey: .status)
        clientMessageID = try values.decodeIfPresent(UUID.self, forKey: .clientMessageID)
        runID = try values.decodeIfPresent(UUID.self, forKey: .runID)
        media = try values.decodeIfPresent([MessageMedia].self, forKey: .media) ?? []
    }
}

public struct MessagesPage: Codable, Equatable, Sendable {
    public let messages: [Message]
    public let nextCursor: Int64?
    public let hasMore: Bool
    public init(messages: [Message], nextCursor: Int64?, hasMore: Bool) {
        self.messages = messages; self.nextCursor = nextCursor; self.hasMore = hasMore
    }
    enum CodingKeys: String, CodingKey {
        case messages
        case nextCursor = "next_cursor", hasMore = "has_more"
    }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        messages = try values.decode([Message].self, forKey: .messages)
        nextCursor = try values.decodeIfPresent(Int64.self, forKey: .nextCursor)
        // A pre-pagination v1 service returns only `messages`; treat it as one complete page.
        hasMore = try values.decodeIfPresent(Bool.self, forKey: .hasMore) ?? false
    }
}

public struct SendMessageResponse: Codable, Equatable, Sendable {
    public let message: Message
    public let runID: UUID
    public let status: String
    enum CodingKeys: String, CodingKey {
        case message, status
        case runID = "run_id"
    }
}

public struct ServiceError: Codable, Equatable, Sendable {
    public let code: String
    public let message: String
    public init(code: String, message: String) { self.code = code; self.message = message }
}

public struct EventPayload: Codable, Equatable, Sendable {
    public let conversation: Conversation?
    public let message: Message?
    public let media: MessageMedia?
    public let runID: UUID?
    public let delta: String?
    public let error: ServiceError?
    public init(conversation: Conversation? = nil, message: Message? = nil,
                media: MessageMedia? = nil, runID: UUID? = nil,
                delta: String? = nil, error: ServiceError? = nil) {
        self.conversation = conversation; self.message = message; self.media = media; self.runID = runID
        self.delta = delta; self.error = error
    }
    enum CodingKeys: String, CodingKey {
        case conversation, message, media, delta, error
        case runID = "run_id"
    }
}

public struct ServerEvent: Codable, Equatable, Sendable, Identifiable {
    public let seq: Int64
    public let type: String
    public let conversationID: UUID?
    public let payload: EventPayload
    public let createdAt: Date
    public var id: Int64 { seq }
    public init(seq: Int64, type: String, conversationID: UUID? = nil,
                payload: EventPayload, createdAt: Date) {
        self.seq = seq; self.type = type; self.conversationID = conversationID
        self.payload = payload; self.createdAt = createdAt
    }
    enum CodingKeys: String, CodingKey {
        case seq, type, payload
        case conversationID = "conversation_id", createdAt = "created_at"
    }
}

public struct EventsPage: Codable, Equatable, Sendable {
    public let events: [ServerEvent]
    public let nextCursor: Int64
    public let hasMore: Bool
    public init(events: [ServerEvent], nextCursor: Int64, hasMore: Bool) {
        self.events = events; self.nextCursor = nextCursor; self.hasMore = hasMore
    }
    enum CodingKeys: String, CodingKey {
        case events
        case nextCursor = "next_cursor", hasMore = "has_more"
    }
}

/// The wire format is explicit: no automatic acronym conversion and no local-time dates.
public enum WireJSON {
    public static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            let zone = #"(?:[zZ]|[+-]\d{2}:\d{2})$"#
            guard value.range(of: zone, options: .regularExpression) != nil else {
                throw DecodingError.dataCorruptedError(in: container, debugDescription: "Timestamp requires a time zone")
            }
            for options: ISO8601DateFormatter.Options in [
                [.withInternetDateTime, .withFractionalSeconds], [.withInternetDateTime],
            ] {
                let formatter = ISO8601DateFormatter()
                formatter.formatOptions = options
                if let date = formatter.date(from: value) { return date }
            }
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Invalid ISO8601 timestamp")
        }
        return decoder
    }
    public static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, encoder in
            let formatter = ISO8601DateFormatter()
            formatter.timeZone = TimeZone(secondsFromGMT: 0)
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            var container = encoder.singleValueContainer()
            try container.encode(formatter.string(from: date))
        }
        return encoder
    }
}
