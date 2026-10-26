import Foundation
import Testing
@testable import YoloAgentsSDK

private let conversationID = UUID(uuidString: "10000000-0000-4000-8000-000000000001")!
private let runID = UUID(uuidString: "20000000-0000-4000-8000-000000000002")!
private let clientID = UUID(uuidString: "30000000-0000-4000-8000-000000000003")!
private let now = Date(timeIntervalSince1970: 1_700_000_000)
private let conversation = Conversation(id: conversationID, title: "测试会话", createdAt: now, updatedAt: now)

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

@Test func queryEncodingAndBearerAreCorrect() async throws {
    let transport = RecordingTransport([HTTPResult(data: Data("{\"conversations\":[]}".utf8), status: 200)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-token", transport: transport)
    _ = try await api.conversations(query: "中文 & AAPL?", includeArchived: true)
    let request = try #require(await transport.requests.first)
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-token")
    let url = try #require(request.url)
    let query = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems)
    #expect(query.first { $0.name == "query" }?.value == "中文 & AAPL?")
    #expect(query.first { $0.name == "include_archived" }?.value == "true")
}

@Test func retryReusesTheSameMessageIDAndContent() async throws {
    let response = SendMessageResponse(message: message(role: .user, content: "你好", clientMessageID: clientID),
                                       runID: runID, status: "duplicate")
    let transport = RecordingTransport([try encoded(response, status: 202), try encoded(response, status: 202)])
    let api = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-token", transport: transport)
    for _ in 0..<2 { _ = try await api.sendMessage(conversationID: conversationID, clientMessageID: clientID, content: "你好") }
    let requests = await transport.requests
    #expect(requests[0].httpBody == requests[1].httpBody)
    #expect(requests[0].httpMethod == "POST")
    let bodyData = try #require(requests[0].httpBody)
    let body = try #require(JSONSerialization.jsonObject(with: bodyData) as? [String: String])
    #expect(body["client_message_id"] == clientID.uuidString)
    #expect(body["content"] == "你好")
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
}

@Test func completionBeforePostResponseStillClearsPendingDelivery() throws {
    var state = ClientState()
    state.enqueue(content: "hello", conversationID: conversationID, id: clientID, now: now)
    let completed = message()
    state = try state.applying(page([event(1, "assistant.completed", EventPayload(message: completed, runID: runID))]))
    try state.accept(SendMessageResponse(message: message(role: .user, clientMessageID: clientID),
                                        runID: runID, status: "duplicate"), clientMessageID: clientID)
    #expect(state.outgoing.isEmpty)
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
