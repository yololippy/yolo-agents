import Foundation
import CryptoKit
import Testing
@testable import YoloAgentsSDK

private let conversationID = UUID(uuidString: "10000000-0000-4000-8000-000000000001")!
private let runID = UUID(uuidString: "20000000-0000-4000-8000-000000000002")!
private let clientID = UUID(uuidString: "30000000-0000-4000-8000-000000000003")!
private let now = Date(timeIntervalSince1970: 1_700_000_000)
private let conversation = Conversation(id: conversationID, title: "测试会话", createdAt: now, updatedAt: now)

private func media(id: UUID = UUID(), filename: String = "chart.png") -> MessageMedia {
    MessageMedia(id: id, mediaType: "image/png", byteCount: 3, sha256: "fixture",
                 filename: filename, createdAt: now)
}

private func message(role: MessageRole = .assistant, content: String = "完整回复", status: MessageStatus = .completed,
                     id: UUID = UUID(), clientMessageID: UUID? = nil, run: UUID? = runID) -> Message {
    Message(id: id, conversationID: conversationID, role: role, content: content, createdAt: now,
            status: status, clientMessageID: clientMessageID, runID: run)
}
private func event(_ seq: Int64, _ type: String, _ payload: EventPayload) -> ServerEvent {
    ServerEvent(seq: seq, type: type, conversationID: conversationID, payload: payload, createdAt: now)
}
private func page(_ events: [ServerEvent], cursor: Int64? = nil, more: Bool = false) -> EventsPage {
    EventsPage(events: events, nextCursor: cursor ?? events.last?.seq ?? 0, hasMore: more)
}
private func encoded<T: Encodable>(_ value: T, status: Int = 200) throws -> HTTPResult {
    HTTPResult(data: try WireJSON.encoder().encode(value), status: status)
}

@Test func agentSessionsBypassSystemProxies() {
    let configuration = directSessionConfiguration(requestTimeout: 12, resourceTimeout: 34)
    #expect(configuration.connectionProxyDictionary?.isEmpty == true)
    #expect(configuration.urlCache == nil)
    #expect(configuration.urlCredentialStorage == nil)
    #expect(configuration.httpCookieStorage == nil)
    #expect(configuration.httpShouldSetCookies == false)
    #expect(configuration.waitsForConnectivity)
    #expect(configuration.allowsCellularAccess)
    #expect(configuration.allowsExpensiveNetworkAccess)
    #expect(configuration.allowsConstrainedNetworkAccess)
    #expect(configuration.timeoutIntervalForRequest == 12)
    #expect(configuration.timeoutIntervalForResource == 34)
}
private actor RecordingTransport: HTTPTransport {
    private var responses: [HTTPResult]
    private(set) var requests: [URLRequest] = []
    init(_ responses: [HTTPResult]) { self.responses = responses }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        guard !responses.isEmpty else { throw URLError(.timedOut) }
        return responses.removeFirst()
    }
}
private final class MemoryStore: SecureStore, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: Data] = [:]
    func read(_ key: String) throws -> Data? { lock.withLock { values[key] } }
    func write(_ data: Data, for key: String) throws { lock.withLock { values[key] = data } }
    func remove(_ key: String) throws { _ = lock.withLock { values.removeValue(forKey: key) } }
}

private func pairing(deviceID: UUID = UUID()) throws -> PairingResponse {
    let json = """
    {"device":{"id":"\(deviceID)","name":"测试手机","platform":"ios","permissions":["chat","read"],
    "created_at":"2026-10-24T12:00:00Z","revoked_at":null},"token":"fixture-token-not-a-secret",
    "server_id":"40000000-0000-4000-8000-000000000004","api_version":1}
    """
    return try WireJSON.decoder().decode(PairingResponse.self, from: Data(json.utf8))
}

@Test func wireUsesSnakeCaseUUIDAndUTC() throws {
    let original = message(clientMessageID: clientID)
    let data = try WireJSON.encoder().encode(original)
    let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    #expect(object["conversation_id"] as? String == conversationID.uuidString)
    #expect(object["client_message_id"] as? String == clientID.uuidString)
    #expect((object["created_at"] as? String)?.hasSuffix("Z") == true)
    #expect(object["conversationID"] == nil)
    #expect(try WireJSON.decoder().decode(Message.self, from: data) == original)
}

@Test func messagesAndOutgoingCacheDecodeBeforeMediaFieldsExisted() throws {
    let oldMessage = """
    {"id":"30000000-0000-4000-8000-000000000003","conversation_id":"\(conversationID)",
    "role":"user","content":"旧缓存","created_at":"2026-10-24T12:00:00Z","status":"pending",
    "client_message_id":"\(clientID)","run_id":null}
    """
    #expect(try WireJSON.decoder().decode(Message.self, from: Data(oldMessage.utf8)).media.isEmpty)

    let oldOutgoing = """
    {"id":"\(clientID)","conversationID":"\(conversationID)","content":"旧缓存",
    "createdAt":"2026-10-24T12:00:00Z","status":"needsRetry","runID":null,"error":"待重试"}
    """
    #expect(try WireJSON.decoder().decode(OutgoingMessage.self, from: Data(oldOutgoing.utf8)).mediaIDs.isEmpty)

    let oldPartial = """
    {"id":"\(runID)","conversationID":"\(conversationID)","content":"旧回复",
    "error":null,"createdAt":"2026-10-24T12:00:00Z"}
    """
    #expect(try WireJSON.decoder().decode(PartialReply.self, from: Data(oldPartial.utf8)).media.isEmpty)
}

@Test(arguments: ["2026-10-24T12:00:00Z", "2026-10-24T12:00:00.123456Z", "2026-10-24T20:00:00+08:00"])
func timestampsWithTimeZonesDecode(_ value: String) throws {
    struct Timestamp: Decodable { let date: Date }
    _ = try WireJSON.decoder().decode(Timestamp.self, from: Data("{\"date\":\"\(value)\"}".utf8))
}

@Test func timestampsWithoutTimeZoneFail() {
    struct Timestamp: Decodable { let date: Date }
    #expect(throws: (any Error).self) {
        try WireJSON.decoder().decode(Timestamp.self, from: Data("{\"date\":\"2026-10-24T12:00:00\"}".utf8))
    }
}

@Test(arguments: ["http://example.com", "http://8.8.8.8", "http://192.168.1.1.evil.example", "http://100.64.0.1",
                  "http://172.32.0.1", "http://[2001:4860:4860::8888]", "http://0127.0.0.1"])
func publicOrAmbiguousHTTPDestinationsFail(_ value: String) {
    #expect(throws: (any Error).self) { try ServiceEndpoint(value, allowInsecureLocalHTTP: true) }
}

@Test(arguments: ["http://127.0.0.1:8765", "http://localhost:8765", "http://10.1.2.3", "http://192.168.1.20",
                  "http://172.16.1.20", "http://[::1]:8765", "http://[fd12:3456::1]:8765"])
func localHTTPRequiresOptIn(_ value: String) throws {
    #expect(throws: ClientError.insecureEndpoint) { try ServiceEndpoint(value) }
    _ = try ServiceEndpoint(value, allowInsecureLocalHTTP: true)
}

@Test(arguments: ["https://user:password@example.com", "https://example.com?token=anything",
                  "https://example.com#fragment", "file:///tmp/data", "example.com", "https://example.com:0"])
func invalidServiceAddressesFail(_ value: String) {
    #expect(throws: (any Error).self) { try ServiceEndpoint(value) }
}

@Test func healthAndPairingDoNotSendExistingBearer() async throws {
    let response = try pairing()
    let health = """
    {"status":"ok","api_version":1,"server_id":"\(response.serverID)","agent_mode":"mock",
    "model":null,"trade_execution_enabled":false}
    """
    let transport = RecordingTransport([HTTPResult(data: Data(health.utf8), status: 200), try encoded(response, status: 201)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com/base"), token: "old-token", transport: transport)
    #expect(try await api.health().tradeExecutionEnabled == false)
    let paired = try await api.pair(code: "a-code", name: "手机", platform: .ios)
    #expect(paired == response)
    let requests = await transport.requests
    #expect(requests.count == 2)
    #expect(requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == nil })
    #expect(requests[0].url?.path == "/base/v1/health")
    let bodyData = try #require(requests[1].httpBody)
    let body = try #require(JSONSerialization.jsonObject(with: bodyData) as? [String: String])
    #expect(body == ["pairing_code": "a-code", "device_name": "手机", "platform": "ios"])
}

@Test func healthDecodesAgentNeutralServiceComponents() throws {
    let json = """
    {"status":"ok","api_version":1,"server_id":"40000000-0000-4000-8000-000000000004",
    "agent_mode":"chatgpt","model":null,"trade_execution_enabled":true,
    "components":[{"id":"ibkr","label":"IBKR Paper","state":"ready","detail":"已连接"}]}
    """
    let health = try WireJSON.decoder().decode(Health.self, from: Data(json.utf8))
    #expect(health.components == [ServiceComponentHealth(
        id: "ibkr", label: "IBKR Paper", state: "ready", detail: "已连接"
    )])
}

@Test func queryEncodingAndBearerAreCorrect() async throws {
    let transport = RecordingTransport([HTTPResult(data: Data("{\"conversations\":[],\"primary_conversation_id\":\"10000000-0000-4000-8000-000000000001\"}".utf8), status: 200)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-token", transport: transport)
    _ = try await api.conversations(query: "中文 & AAPL?", includeArchived: true)
    let request = try #require(await transport.requests.first)
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-token")
    let url = try #require(request.url)
    let query = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems)
    #expect(query.first { $0.name == "query" }?.value == "中文 & AAPL?")
    #expect(query.first { $0.name == "include_archived" }?.value == "true")
}

@Test func legacyConversationListWithoutPrimaryRemainsReadable() async throws {
    let legacy = """
    {"conversations":[{"id":"10000000-0000-4000-8000-000000000002","title":"旧会话",
    "created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z","archived":false,"model":null}]}
    """
    let transport = RecordingTransport([HTTPResult(data: Data(legacy.utf8), status: 200)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-token", transport: transport)
    let list = try await api.conversationList(includeArchived: true)
    #expect(list.conversations.map(\.title) == ["旧会话"])
    #expect(list.primaryConversationID == nil)
}

@Test func messageHistoryUsesBoundedBackwardCursorPages() async throws {
    let older = message(role: .user, content: "older", id: UUID(), run: nil)
    let page = MessagesPage(messages: [older], nextCursor: 42, hasMore: true)
    let legacy = """
    {"messages":[]}
    """
    let transport = RecordingTransport([
        try encoded(page), HTTPResult(data: Data(legacy.utf8), status: 200),
    ])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com/base"),
                        token: "fixture-token", transport: transport)
    #expect(try await api.messagePage(
        conversationID: conversationID, before: 99, limit: 500
    ) == page)
    #expect(try await api.messagePage(conversationID: conversationID) ==
            MessagesPage(messages: [], nextCursor: nil, hasMore: false))

    let requests = await transport.requests
    let first = try #require(URLComponents(url: requests[0].url!, resolvingAgainstBaseURL: false))
    #expect(first.path == "/base/v1/conversations/\(conversationID.uuidString)/messages")
    #expect(first.queryItems?.first(where: { $0.name == "before" })?.value == "99")
    #expect(first.queryItems?.first(where: { $0.name == "limit" })?.value == "200")
    #expect(requests[1].url?.query?.contains("limit=50") == true)
}

@Test func invalidMessageCursorPagesAreRejected() async throws {
    let invalid = MessagesPage(messages: [], nextCursor: nil, hasMore: true)
    let nonDecreasing = MessagesPage(messages: [], nextCursor: 100, hasMore: true)
    let transport = RecordingTransport([try encoded(invalid), try encoded(nonDecreasing)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"),
                        token: "fixture-token", transport: transport)
    await #expect(throws: ClientError.invalidMessagePage) {
        try await api.messagePage(conversationID: conversationID)
    }
    await #expect(throws: ClientError.invalidMessagePage) {
        try await api.messagePage(conversationID: conversationID, before: 0)
    }
    await #expect(throws: ClientError.invalidMessagePage) {
        try await api.messagePage(conversationID: conversationID, before: 100)
    }
}

@Test func retryReusesTheSameMessageIDAndContent() async throws {
    let response = SendMessageResponse(message: message(role: .user, content: "你好", clientMessageID: clientID),
                                       runID: runID, status: "duplicate")
    let transport = RecordingTransport([try encoded(response, status: 202), try encoded(response, status: 202)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-token", transport: transport)
    for _ in 0..<2 { _ = try await api.sendMessage(conversationID: conversationID, clientMessageID: clientID, content: "你好") }
    let requests = await transport.requests
    let firstData = try #require(requests[0].httpBody)
    let secondData = try #require(requests[1].httpBody)
    let firstBody = try #require(JSONSerialization.jsonObject(with: firstData) as? [String: Any])
    let secondBody = try #require(JSONSerialization.jsonObject(with: secondData) as? [String: Any])
    #expect(NSDictionary(dictionary: firstBody).isEqual(to: secondBody))
    #expect(requests[0].httpMethod == "POST")
    #expect(firstBody["client_message_id"] as? String == clientID.uuidString)
    #expect(firstBody["content"] as? String == "你好")
    #expect(firstBody["media_ids"] as? [String] == [])
}

@Test func mediaUploadDownloadAndMessageReferencesUseAuthenticatedV1Contract() async throws {
    let mediaID = UUID(uuidString: "60000000-0000-4000-8000-000000000006")!
    let bytes = Data([0xff, 0xd8, 0xff, 0xd9])
    let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    let metadata = MessageMedia(id: mediaID, mediaType: "image/jpeg", byteCount: bytes.count,
                                sha256: digest, filename: "photo.jpg", createdAt: now)
    struct UploadResponse: Encodable { let media: MessageMedia }
    let canonical = Message(id: UUID(), conversationID: conversationID, role: .user, content: "",
                            createdAt: now, status: .completed, clientMessageID: clientID, media: [metadata])
    let response = SendMessageResponse(message: canonical, runID: runID, status: "accepted")
    let transport = RecordingTransport([
        try encoded(UploadResponse(media: metadata), status: 201),
        try encoded(response, status: 202),
        HTTPResult(data: bytes, status: 200),
    ])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com/base"),
                        token: "fixture-token", transport: transport)

    #expect(try await api.uploadMedia(clientMediaID: mediaID, data: bytes,
                                     mediaType: "image/jpeg", filename: "photo.jpg") == metadata)
    _ = try await api.sendMessage(conversationID: conversationID, clientMessageID: clientID,
                                  content: "", mediaIDs: [mediaID])
    #expect(try await api.mediaData(metadata) == bytes)

    let requests = await transport.requests
    #expect(requests.map { $0.url?.path } == [
        "/base/v1/media",
        "/base/v1/conversations/\(conversationID.uuidString)/messages",
        "/base/v1/media/\(mediaID.uuidString)",
    ])
    #expect(requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-token" })
    #expect(requests[0].httpBody == bytes)
    #expect(requests[0].value(forHTTPHeaderField: "Content-Type") == "image/jpeg")
    #expect(requests[0].value(forHTTPHeaderField: "Content-Length") == String(bytes.count))
    #expect(requests[0].value(forHTTPHeaderField: "X-Client-Media-ID") == mediaID.uuidString)
    let body = try #require(JSONSerialization.jsonObject(with: requests[1].httpBody ?? Data()) as? [String: Any])
    #expect(body["content"] as? String == "")
    #expect(body["media_ids"] as? [String] == [mediaID.uuidString])
}

@Test func mediaLimitsRejectInvalidDraftsBeforeNetwork() async throws {
    let transport = RecordingTransport([])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"),
                        token: "fixture-token", transport: transport)
    await #expect(throws: ClientError.invalidMediaType) {
        try await api.uploadMedia(clientMediaID: UUID(), data: Data([1]), mediaType: "image/heic")
    }
    #expect(throws: ClientError.emptyMessage) {
        try APIClient.validateMessage(content: " \n", mediaIDs: [])
    }
    #expect(throws: ClientError.tooManyMediaItems) {
        try APIClient.validateMessage(content: "", mediaIDs: (0...MediaLimits.maximumItemsPerMessage).map { _ in UUID() })
    }
    #expect(await transport.requests.isEmpty)
}

@Test func mediaUploadRejectsAResponseWithTheWrongId() async throws {
    let requestedID = UUID()
    let metadata = MessageMedia(id: UUID(), mediaType: "image/png", byteCount: 3,
                                sha256: "fixture", createdAt: now)
    struct UploadResponse: Encodable { let media: MessageMedia }
    let transport = RecordingTransport([try encoded(UploadResponse(media: metadata), status: 201)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"),
                        token: "fixture-token", transport: transport)

    await #expect(throws: ClientError.invalidMediaResponse) {
        try await api.uploadMedia(clientMediaID: requestedID, data: Data([1, 2, 3]),
                                  mediaType: "image/png")
    }
}

@Test func missingTokenDoesNotReachTransport() async throws {
    let transport = RecordingTransport([])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"), transport: transport)
    do { _ = try await api.conversations(); Issue.record("Unauthenticated call succeeded") }
    catch let error as ClientError { #expect(error.requiresPairing) }
    #expect(await transport.requests.isEmpty)
}

@Test func messageLimitsCountUnicodeScalarsAndEncodedBody() {
    #expect(throws: ClientError.messageTooLong) {
        try APIClient.validateMessageContent(String(repeating: "e\u{301}", count: 8_001))
    }
    #expect(throws: ClientError.messageTooLong) {
        try APIClient.validateMessageContent(String(repeating: "\u{01}", count: 16_000))
    }
    #expect(throws: Never.self) { try APIClient.validateMessageContent("你好，AAPL") }
}

@Test func revokedTokenPropagatesAsAuthenticationError() async throws {
    let data = Data("{\"error\":{\"code\":\"device_revoked\",\"message\":\"设备已撤销\"}}".utf8)
    let transport = RecordingTransport([HTTPResult(data: data, status: 401)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-token", transport: transport)
    do { _ = try await api.events(after: 0); Issue.record("Revoked call succeeded") }
    catch let error as ClientError {
        #expect(error == .http(status: 401, code: "device_revoked", message: "设备已撤销"))
        #expect(error.requiresPairing)
    }
}

@Test func paginatedEventsAndReplayDoNotDuplicateDeltas() throws {
    var state = ClientState()
    let first = page([event(1, "assistant.delta", EventPayload(runID: runID, delta: "你"))], more: true)
    state = try state.applying(first)
    state = try state.applying(page(first.events))
    #expect(state.partialReplies.first?.content == "你")
    state = try state.applying(page([event(2, "assistant.delta", EventPayload(runID: runID, delta: "好"))]))
    #expect(state.cursor == 2)
    #expect(state.partialReplies.first?.content == "你好")
    let completed = message(content: "你好！")
    state = try state.applying(page([event(3, "assistant.completed", EventPayload(message: completed, runID: runID))]))
    #expect(state.partialReplies.isEmpty)
    #expect(state.messages(in: conversationID) == [completed])
}

@Test func assistantMediaMergesByIdAndCompletionRemainsAuthoritative() throws {
    let firstID = UUID(uuidString: "60000000-0000-4000-8000-000000000061")!
    let secondID = UUID(uuidString: "60000000-0000-4000-8000-000000000062")!
    let first = media(id: firstID, filename: "preview.png")
    let second = media(id: secondID, filename: "final.png")
    var state = ClientState()

    state = try state.applying(page([
        event(1, "assistant.media", EventPayload(media: first, runID: runID)),
        event(2, "assistant.media", EventPayload(media: first, runID: runID)),
        event(3, "assistant.media", EventPayload(media: second, runID: runID)),
    ]))
    #expect(state.partialReplies.first?.media == [first, second])
    #expect(state.partialReplies.first?.content == "")

    let completed = Message(id: UUID(), conversationID: conversationID, role: .assistant,
                            content: "完成", createdAt: now, status: .completed,
                            runID: runID, media: [second])
    state = try state.applying(page([
        event(4, "assistant.completed", EventPayload(message: completed, runID: runID)),
    ]))
    #expect(state.partialReplies.isEmpty)
    #expect(state.messages(in: conversationID).first?.media == [second])
}

@Test func assistantMediaRequiresRunConversationAndMedia() throws {
    let item = media()
    #expect(throws: ClientError.invalidEvent("assistant.media")) {
        try ClientState().applying(page([
            event(1, "assistant.media", EventPayload(media: item)),
        ]))
    }
    #expect(throws: ClientError.invalidEvent("assistant.media")) {
        try ClientState().applying(page([
            event(1, "assistant.media", EventPayload(runID: runID)),
        ]))
    }
    let missingConversation = ServerEvent(seq: 1, type: "assistant.media", conversationID: nil,
                                          payload: EventPayload(media: item, runID: runID), createdAt: now)
    #expect(throws: ClientError.invalidEvent("assistant.media")) {
        try ClientState().applying(EventsPage(events: [missingConversation], nextCursor: 1, hasMore: false))
    }

    var state = try ClientState().applying(page([
        event(1, "assistant.media", EventPayload(media: item, runID: runID)),
    ]))
    let otherConversationID = UUID()
    let mismatched = ServerEvent(seq: 2, type: "assistant.media", conversationID: otherConversationID,
                                 payload: EventPayload(media: media(), runID: runID), createdAt: now)
    #expect(throws: ClientError.invalidEvent("assistant.media")) {
        state = try state.applying(EventsPage(events: [mismatched], nextCursor: 2, hasMore: false))
    }
}

@Test func invalidPagesAreAtomicAndDoNotAdvance() throws {
    let state = ClientState()
    let delta = event(1, "assistant.delta", EventPayload(runID: runID, delta: "partial"))
    let malformed = event(2, "assistant.completed", EventPayload(runID: runID))
    #expect(throws: (any Error).self) { try state.applying(page([delta, malformed])) }
    #expect(throws: ClientError.invalidEventPage) { try state.applying(page([delta], cursor: 99)) }
    #expect(throws: ClientError.invalidEventPage) { try state.applying(page([delta, delta])) }
    #expect(throws: ClientError.invalidEventPage) { try state.applying(page([], more: true)) }
    #expect(state.cursor == 0 && state.partialReplies.isEmpty)
}

@Test func unknownEventsAndSequenceGapsAreForwardCompatible() throws {
    let next = try ClientState().applying(page([event(27, "future.notification", EventPayload())]))
    #expect(next.cursor == 27)
    #expect(next.messages(in: conversationID).isEmpty)
}

@Test func optimisticMessageIsReplacedByCanonicalServerMessage() throws {
    var state = ClientState()
    state.enqueue(content: "hello", conversationID: conversationID, id: clientID, now: now)
    let canonical = message(role: .user, content: "hello", clientMessageID: clientID)
    try state.accept(SendMessageResponse(message: canonical, runID: runID, status: "accepted"), clientMessageID: clientID)
    #expect(state.messages(in: conversationID) == [canonical])
    #expect(state.outgoing.first?.status == .accepted)
    #expect(state.outgoing.first?.runID == runID)
    #expect(state.awaitingReplies(in: conversationID).map(\.id) == [runID])
}

@Test func acceptedRunsWaitOnlyUntilTheirFirstAssistantEventAndStayConversationScoped() throws {
    let secondRunID = UUID(uuidString: "20000000-0000-4000-8000-000000000003")!
    let secondClientID = UUID(uuidString: "30000000-0000-4000-8000-000000000004")!
    let otherConversationID = UUID(uuidString: "10000000-0000-4000-8000-000000000005")!
    let otherRunID = UUID(uuidString: "20000000-0000-4000-8000-000000000006")!
    let otherClientID = UUID(uuidString: "30000000-0000-4000-8000-000000000007")!
    var state = ClientState()

    for (client, run, createdAt) in [
        (clientID, runID, now),
        (secondClientID, secondRunID, now.addingTimeInterval(1)),
    ] {
        state.enqueue(content: "hello", conversationID: conversationID, id: client, now: createdAt)
        let canonical = Message(id: UUID(), conversationID: conversationID, role: .user, content: "hello",
                                createdAt: createdAt, status: .completed, clientMessageID: client)
        try state.accept(SendMessageResponse(message: canonical, runID: run, status: "accepted"), clientMessageID: client)
    }
    state.enqueue(content: "other", conversationID: otherConversationID, id: otherClientID, now: now)
    let otherCanonical = Message(id: UUID(), conversationID: otherConversationID, role: .user, content: "other",
                                 createdAt: now, status: .completed, clientMessageID: otherClientID)
    try state.accept(SendMessageResponse(message: otherCanonical, runID: otherRunID, status: "accepted"),
                     clientMessageID: otherClientID)

    #expect(state.awaitingReplies(in: conversationID).map(\.id) == [runID, secondRunID])
    #expect(state.awaitingReplies(in: otherConversationID).map(\.id) == [otherRunID])

    state = try state.applying(page([event(1, "assistant.delta", EventPayload(runID: runID, delta: "你"))]))
    #expect(state.awaitingReplies(in: conversationID).map(\.id) == [secondRunID])
    #expect(state.partialReplies.map(\.id).contains(runID))

    let failed = ServerEvent(seq: 2, type: "assistant.failed", conversationID: conversationID,
                             payload: EventPayload(runID: secondRunID,
                                                   error: ServiceError(code: "failed", message: "失败")),
                             createdAt: now)
    state = try state.applying(EventsPage(events: [failed], nextCursor: 2, hasMore: false))
    #expect(state.awaitingReplies(in: conversationID).isEmpty)
}

@Test func completionBeforePostResponseStillClearsPendingDelivery() throws {
    var state = ClientState()
    state.enqueue(content: "hello", conversationID: conversationID, id: clientID, now: now)
    let completed = message()
    state = try state.applying(page([event(1, "assistant.completed", EventPayload(message: completed, runID: runID))]))
    #expect(state.awaitingReplies(in: conversationID).isEmpty)
    try state.accept(SendMessageResponse(message: message(role: .user, clientMessageID: clientID),
                                        runID: runID, status: "duplicate"), clientMessageID: clientID)
    #expect(state.outgoing.isEmpty)
    #expect(state.awaitingReplies(in: conversationID).isEmpty)
    #expect(state.messages(in: conversationID).count == 2)
}

@Test func assistantFailureKeepsPartialAndIsNeverSuccess() throws {
    var state = ClientState()
    let failure = ServiceError(code: "model_timeout", message: "模型超时")
    state = try state.applying(page([
        event(1, "assistant.delta", EventPayload(runID: runID, delta: "未完成")),
        event(2, "assistant.failed", EventPayload(runID: runID, error: failure)),
    ]))
    #expect(state.partialReplies.first?.content == "未完成")
    #expect(state.partialReplies.first?.error == failure)
    #expect(state.messages(in: conversationID).isEmpty)
    state = try state.applying(page([event(3, "assistant.delta", EventPayload(runID: runID, delta: "不应拼接"))]))
    #expect(state.partialReplies.first?.content == "未完成")
}

@Test func terminalMessagesCannotRegressToPendingFromOlderSnapshot() throws {
    var state = ClientState()
    let final = message()
    let stale = message(content: "old", status: .pending, id: final.id)
    try state.mergeMessages([final, stale], conversationID: conversationID)
    #expect(state.messages(in: conversationID) == [final])
}

@Test func failedMessagesCannotBecomeSuccessfulFromConflictingSnapshot() throws {
    var state = ClientState()
    let failed = message(content: "回复生成失败", status: .failed)
    let conflicting = message(content: "错误成功状态", status: .completed, id: failed.id)
    try state.mergeMessages([failed, conflicting], conversationID: conversationID)
    #expect(state.messages(in: conversationID) == [failed])
}

@Test func authoritativeFailureMessageReplacesPartial() throws {
    var state = try ClientState().applying(page([event(1, "assistant.delta", EventPayload(runID: runID, delta: "片段"))]))
    let failed = message(content: "模型调用失败", status: .failed)
    state = try state.applying(page([event(2, "assistant.failed", EventPayload(message: failed, runID: runID,
        error: ServiceError(code: "agent_failed", message: "模型调用失败")))]))
    #expect(state.partialReplies.isEmpty)
    #expect(state.messages(in: conversationID) == [failed])
}

@Test func wrongConversationAndMismatchedCompletionFail() {
    let bad = Message(id: UUID(), conversationID: UUID(), role: .assistant, content: "wrong",
                      createdAt: now, status: .completed, runID: runID)
    #expect(throws: (any Error).self) {
        try ClientState().applying(page([event(1, "assistant.completed", EventPayload(message: bad, runID: runID))]))
    }
    #expect(throws: (any Error).self) {
        try ClientState().applying(page([event(1, "assistant.completed", EventPayload(message: message(status: .failed), runID: runID))]))
    }
}

@Test func cacheSurvivesRestartWithoutAutomaticallyResending() throws {
    let repository = ClientRepository(store: MemoryStore())
    let connection = PairedConnection(endpoint: try ServiceEndpoint("https://example.com"), pairing: try pairing())
    try repository.saveConnection(connection)
    var state = ClientState()
    state.upsertConversation(conversation)
    state.enqueue(content: "尚未确认", conversationID: conversationID, id: clientID, now: now)
    state = try state.applying(page([event(5, "future.event", EventPayload())]))
    try repository.saveState(state, for: connection)
    #expect(try repository.connection() == connection)
    let restored = try repository.state(for: connection)
    #expect(restored.cursor == 5)
    #expect(restored.messages(in: conversationID).first?.content == "尚未确认")
    #expect(restored.outgoing.first?.id == clientID)
    #expect(restored.outgoing.first?.status == .needsRetry)
    let other = PairedConnection(endpoint: connection.endpoint, pairing: try pairing())
    #expect(try repository.state(for: other) == ClientState())
    try repository.forget(connection)
    #expect(try repository.connection() == nil)
    #expect(try repository.state(for: connection) == ClientState())
}

@Test func primaryConversationRemainsFirstWhenNewerConversationsArrive() {
    let primaryID = UUID(uuidString: "10000000-0000-4000-8000-000000000070")!
    let newerID = UUID(uuidString: "10000000-0000-4000-8000-000000000071")!
    var state = ClientState()
    state.upsertConversation(Conversation(
        id: primaryID, title: "main", createdAt: now,
        updatedAt: now
    ))
    state.upsertConversation(Conversation(
        id: newerID, title: "新会话", createdAt: now.addingTimeInterval(60),
        updatedAt: now.addingTimeInterval(60)
    ))

    #expect(state.conversations.first?.id == newerID)
    state.setPrimaryConversation(primaryID)
    #expect(state.conversations.map(\.id) == [primaryID, newerID])

    state.upsertConversation(Conversation(
        id: newerID, title: "再次更新的新会话", createdAt: now.addingTimeInterval(60),
        updatedAt: now.addingTimeInterval(120)
    ))
    #expect(state.orderedConversations.map(\.id) == [primaryID, newerID])
    #expect(state.cacheSnapshot().conversations.first?.id == primaryID)
}

@Test func boundedCacheRetainsOutstandingRequestAndRecentMessages() throws {
    var state = ClientState()
    for i in 0..<60 {
        let date = now.addingTimeInterval(Double(i))
        state.upsertConversation(Conversation(id: i == 0 ? conversationID : UUID(), title: "\(i)", createdAt: date, updatedAt: date))
    }
    state.enqueue(content: "待核对", conversationID: conversationID, id: clientID, now: now)
    try state.mergeMessages((0..<350).map { i in
        Message(id: UUID(), conversationID: conversationID, role: .assistant, content: "\(i)",
                createdAt: now.addingTimeInterval(Double(i + 1)), status: .completed)
    }, conversationID: conversationID)
    let snapshot = state.cacheSnapshot()
    #expect(snapshot.conversations.count == 51)
    #expect(snapshot.messages(in: conversationID).count == 301)
    #expect(snapshot.messages(in: conversationID).contains { $0.clientMessageID == clientID })
    #expect(snapshot.outgoing.first?.id == clientID)
}

@Test func cacheTextBudgetIsBounded() throws {
    var state = ClientState()
    state.upsertConversation(conversation)
    try state.mergeMessages((0..<100).map { i in
        Message(id: UUID(), conversationID: conversationID, role: .assistant,
                content: String(repeating: "中", count: 16_000), createdAt: now.addingTimeInterval(Double(i)), status: .completed)
    }, conversationID: conversationID)
    let cache = state.cacheSnapshot()
    #expect(cache.messages(in: conversationID).count < 100)
    #expect(try WireJSON.encoder().encode(cache).count < 1_600_000)
}
