import Foundation

public enum ClientError: Error, LocalizedError, Equatable, Sendable {
    case invalidEndpoint
    case insecureEndpoint
    case messageTooLong
    case nonHTTPResponse
    case http(status: Int, code: String?, message: String)
    case incompatibleServer
    case invalidEventPage
    case invalidEvent(String)
    public var errorDescription: String? {
        switch self {
        case .invalidEndpoint: "服务地址无效，请填写完整地址，且不要包含用户名、密码、参数或片段。"
        case .insecureEndpoint: "请使用 HTTPS；开发 HTTP 需要手动开启，并且只允许本机或私有 IP 地址。"
        case .messageTooLong: "消息过长，请控制在 16000 个字符和请求大小限制内。"
        case .nonHTTPResponse: "服务未返回有效的 HTTP 响应。"
        case .http(let status, _, let message): "服务请求失败（\(status)）：\(message)"
        case .incompatibleServer: "服务身份或协议版本不匹配，请核对地址并重新配对。"
        case .invalidEventPage: "同步数据的顺序或游标无效，已停止推进游标。"
        case .invalidEvent(let type): "同步事件缺少必要字段：\(type)"
        }
    }
    public var requiresPairing: Bool {
        if case .http(let status, _, _) = self { return status == 401 || status == 403 }
        return false
    }
}

public struct ServiceEndpoint: Codable, Equatable, Sendable {
    public let url: URL
    public let allowInsecureLocalHTTP: Bool
    public init(_ value: String, allowInsecureLocalHTTP: Bool = false) throws {
        guard let components = URLComponents(string: value.trimmingCharacters(in: .whitespacesAndNewlines)),
              let url = components.url, let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              components.port.map({ (1...65535).contains($0) }) ?? true,
              ["http", "https"].contains(components.scheme?.lowercased() ?? "") else {
            throw ClientError.invalidEndpoint
        }
        if components.scheme?.lowercased() == "http" {
            guard allowInsecureLocalHTTP, Self.isPrivateLiteral(host) else {
                throw ClientError.insecureEndpoint
            }
        }
        self.url = url; self.allowInsecureLocalHTTP = allowInsecureLocalHTTP
    }
    // Hostnames other than localhost are deliberately excluded for HTTP: DNS must not
    // redirect a device credential onto a public destination. HTTPS supports hostnames.
    private static func isPrivateLiteral(_ rawHost: String) -> Bool {
        let host = rawHost.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if host == "localhost" || host == "::1" { return true }
        if host.contains(":"), !host.contains("%"),
           host.range(of: #"^[0-9a-f:]+$"#, options: .regularExpression) != nil {
            return host.hasPrefix("fc") || host.hasPrefix("fd")
        }
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4, parts.allSatisfy({
            !$0.isEmpty && $0.allSatisfy(\.isNumber) &&
                Int($0).map { (0...255).contains($0) } == true &&
                ($0.count == 1 || !$0.hasPrefix("0"))
        }) else { return false }
        let bytes = parts.compactMap { Int($0) }
        return bytes[0] == 127 || bytes[0] == 10 ||
            (bytes[0] == 172 && (16...31).contains(bytes[1])) ||
            (bytes[0] == 192 && bytes[1] == 168)
    }
    enum CodingKeys: String, CodingKey { case url, allowInsecureLocalHTTP }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        try self.init(values.decode(URL.self, forKey: .url).absoluteString,
                      allowInsecureLocalHTTP: values.decode(Bool.self, forKey: .allowInsecureLocalHTTP))
    }
}

public struct HTTPResult: Sendable {
    public let data: Data
    public let status: Int
    public init(data: Data, status: Int) { self.data = data; self.status = status }
}

public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> HTTPResult
}

private final class NoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

/// No persistent HTTP cache, cookies, credential store, redirects, or TLS bypass.
public struct URLSessionTransport: HTTPTransport {
    private let session: URLSession
    public init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 45
        self.session = URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }
    public func send(_ request: URLRequest) async throws -> HTTPResult {
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ClientError.nonHTTPResponse }
        return HTTPResult(data: data, status: response.statusCode)
    }
}

private struct ErrorEnvelope: Decodable { let error: ServiceError?; let detail: String? }
private struct MessageSubmission: Encodable { let client_message_id: UUID; let content: String }

public struct APIClient: Sendable {
    public let endpoint: ServiceEndpoint
    private let token: String?
    private let transport: any HTTPTransport
    public init(endpoint: ServiceEndpoint, token: String? = nil,
                transport: any HTTPTransport = URLSessionTransport()) {
        self.endpoint = endpoint; self.token = token; self.transport = transport
    }
    public func health() async throws -> Health {
        try await request("GET", path: "health", authenticated: false)
    }
    public func pair(code: String, name: String, platform: DevicePlatform) async throws -> PairingResponse {
        struct Body: Encodable {
            let pairing_code: String
            let device_name: String
            let platform: DevicePlatform
        }
        return try await request("POST", path: "devices/pair", body: WireJSON.encoder().encode(
            Body(pairing_code: code, device_name: name, platform: platform)), authenticated: false)
    }
    /// The service verifies this is the caller's own device (or a Mac administrator).
    /// No device-management UI is exposed on the iPhone client.
    public func revokeOwnDevice(_ id: UUID) async throws {
        struct Response: Decodable { let device: Device }
        let _: Response = try await request("DELETE", path: "devices/\(id.uuidString)")
    }
    public func conversations(query: String? = nil, includeArchived: Bool = false) async throws -> [Conversation] {
        struct Response: Decodable { let conversations: [Conversation] }
        var queryItems = [URLQueryItem(name: "include_archived", value: String(includeArchived))]
        if let query, !query.isEmpty { queryItems.append(URLQueryItem(name: "query", value: query)) }
        let response: Response = try await request("GET", path: "conversations", query: queryItems)
        return response.conversations
    }
    public func createConversation(title: String) async throws -> Conversation {
        struct Body: Encodable { let title: String }
        struct Response: Decodable { let conversation: Conversation }
        let response: Response = try await request("POST", path: "conversations",
            body: WireJSON.encoder().encode(Body(title: title)))
        return response.conversation
    }
    public func updateConversation(_ id: UUID, title: String? = nil, archived: Bool? = nil) async throws -> Conversation {
        struct Body: Encodable { let title: String?; let archived: Bool? }
        struct Response: Decodable { let conversation: Conversation }
        let response: Response = try await request("PATCH", path: "conversations/\(id.uuidString)",
            body: WireJSON.encoder().encode(Body(title: title, archived: archived)))
        return response.conversation
    }
    public func messages(conversationID: UUID) async throws -> [Message] {
        struct Response: Decodable { let messages: [Message] }
        let response: Response = try await request("GET", path: "conversations/\(conversationID.uuidString)/messages")
        return response.messages
    }
    public func sendMessage(conversationID: UUID, clientMessageID: UUID, content: String) async throws -> SendMessageResponse {
        try Self.validateMessageContent(content)
        return try await request("POST", path: "conversations/\(conversationID.uuidString)/messages",
            body: WireJSON.encoder().encode(MessageSubmission(client_message_id: clientMessageID, content: content)))
    }
    public static func validateMessageContent(_ content: String) throws {
        // Python's len(str) counts Unicode scalars, while Swift's String.count counts
        // extended grapheme clusters. Check the server boundary before persisting an intent.
        let body = try WireJSON.encoder().encode(MessageSubmission(client_message_id: UUID(), content: content))
        guard content.unicodeScalars.count <= 16_000, body.count <= 65_536 else { throw ClientError.messageTooLong }
    }
    /// Global polling is the v1 compatibility transport. Always drain has_more before waiting.
    public func events(after cursor: Int64, limit: Int = 200, conversationID: UUID? = nil) async throws -> EventsPage {
        var query = [URLQueryItem(name: "after", value: String(cursor)),
                     URLQueryItem(name: "limit", value: String(min(200, max(1, limit))))]
        if let conversationID { query.append(URLQueryItem(name: "conversation_id", value: conversationID.uuidString)) }
        return try await request("GET", path: "events", query: query)
    }
    private func request<Response: Decodable>(_ method: String, path: String,
        query: [URLQueryItem] = [], body: Data? = nil, authenticated: Bool = true) async throws -> Response {
        var components = URLComponents(url: endpoint.url.appendingPathComponent("v1").appendingPathComponent(path),
                                       resolvingAgainstBaseURL: false)!
        if !query.isEmpty { components.queryItems = query }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if authenticated {
            guard let token, !token.isEmpty else {
                throw ClientError.http(status: 401, code: "not_paired", message: "请先配对此设备。")
            }
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        let result = try await transport.send(request)
        guard (200...299).contains(result.status) else {
            let envelope = try? WireJSON.decoder().decode(ErrorEnvelope.self, from: result.data)
            throw ClientError.http(status: result.status, code: envelope?.error?.code,
                message: envelope?.error?.message ?? envelope?.detail ?? "请求未完成，请稍后重试。")
        }
        return try WireJSON.decoder().decode(Response.self, from: result.data)
    }
}
