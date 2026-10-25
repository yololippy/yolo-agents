import Foundation

public enum DevicePlatform: String, Codable, Sendable { case ios, macos }
public enum MessageRole: String, Codable, Sendable { case user, assistant, system }
public enum MessageStatus: String, Codable, Sendable { case pending, completed, failed }

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

public struct Health: Codable, Equatable, Sendable {
    public let status: String
    public let apiVersion: Int
    public let serverID: UUID
    public let agentMode: String
    public let model: String?
    public let tradeExecutionEnabled: Bool
    enum CodingKeys: String, CodingKey {
        case status, model
        case apiVersion = "api_version", serverID = "server_id"
        case agentMode = "agent_mode", tradeExecutionEnabled = "trade_execution_enabled"
    }
}

public struct Conversation: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public var title: String
    public let createdAt: Date
    public var updatedAt: Date
    public var archived: Bool
    public init(id: UUID, title: String, createdAt: Date, updatedAt: Date, archived: Bool = false) {
        self.id = id; self.title = title; self.createdAt = createdAt
        self.updatedAt = updatedAt; self.archived = archived
    }
    enum CodingKeys: String, CodingKey {
        case id, title, archived
        case createdAt = "created_at", updatedAt = "updated_at"
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
    public init(id: UUID, conversationID: UUID, role: MessageRole, content: String,
                createdAt: Date, status: MessageStatus, clientMessageID: UUID? = nil, runID: UUID? = nil) {
        self.id = id; self.conversationID = conversationID; self.role = role
        self.content = content; self.createdAt = createdAt; self.status = status
        self.clientMessageID = clientMessageID; self.runID = runID
    }
    enum CodingKeys: String, CodingKey {
        case id, role, content, status
        case conversationID = "conversation_id", createdAt = "created_at"
        case clientMessageID = "client_message_id", runID = "run_id"
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
    public let runID: UUID?
    public let delta: String?
    public let error: ServiceError?
    public init(conversation: Conversation? = nil, message: Message? = nil, runID: UUID? = nil,
                delta: String? = nil, error: ServiceError? = nil) {
        self.conversation = conversation; self.message = message; self.runID = runID
        self.delta = delta; self.error = error
    }
    enum CodingKeys: String, CodingKey {
        case conversation, message, delta, error
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
