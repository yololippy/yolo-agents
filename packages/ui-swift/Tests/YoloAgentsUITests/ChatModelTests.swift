import Foundation
import Testing
import YoloAgentsSDK
@testable import YoloAgentsUI

private let testConversationID = UUID(uuidString: "10000000-0000-4000-8000-000000000001")!
private let testRunID = UUID(uuidString: "20000000-0000-4000-8000-000000000002")!
private let serverID = "40000000-0000-4000-8000-000000000004"
private let conversationJSON = """
{"id":"\(testConversationID)","title":"Fixture","model":"model-a","archived":false,
"created_at":"2026-10-27T00:00:00Z","updated_at":"2026-10-27T00:00:00Z"}
"""

@Test func everyRenameRequestStartsWithTheLatestConversationTitle() {
    let now = Date()
    var conversation = Conversation(id: testConversationID, title: "第一次名称", createdAt: now, updatedAt: now)
    let first = ConversationRenameRequest(conversation)
    conversation.title = "第二次名称"
    let second = ConversationRenameRequest(conversation)

    #expect(first.initialTitle == "第一次名称")
    #expect(second.initialTitle == "第二次名称")
    #expect(first.id != second.id)
}
private final class UIStore: SecureStore, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: Data] = [:]
    func read(_ key: String) throws -> Data? { lock.withLock { values[key] } }
    func write(_ data: Data, for key: String) throws { lock.withLock { values[key] = data } }
    func remove(_ key: String) throws { _ = lock.withLock { values.removeValue(forKey: key) } }
}
private final class UIFLAG: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.withLock { value = true } }
    var isSet: Bool { lock.withLock { value } }
}
private actor UIHTTP: HTTPTransport {
    let catalogError: Bool
    let rejectModel: Bool
    private var missedEvent: ServerEvent?
    private var messageErrorCode: URLError.Code?
    private(set) var cursors: [Int64] = []
    init(catalogError: Bool = false, rejectModel: Bool = false) {
        self.catalogError = catalogError; self.rejectModel = rejectModel
    }
    func setMissed(_ event: ServerEvent) { missedEvent = event }
    func setMessageError(_ code: URLError.Code?) { messageErrorCode = code }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let path = request.url!.path
        let json: String
        if path == "/v1/health" {
            json = "{\"status\":\"ok\",\"api_version\":1,\"server_id\":\"\(serverID)\",\"agent_mode\":\"codex\",\"model\":null,\"trade_execution_enabled\":false}"
        } else if path == "/v1/models" {
            json = catalogError ? "{\"models\":[],\"default_model\":null,\"error\":{\"code\":\"codex_unavailable\",\"message\":\"服务端登录需要处理\"}}" :
                "{\"models\":[{\"id\":\"model-a\",\"display_name\":\"Model A\"},{\"id\":\"model-b\",\"display_name\":\"Model B\"}],\"default_model\":\"model-a\",\"auth\":{\"status\":\"authenticated\"}}"
        } else if path == "/v1/conversations" {
            json = "{\"conversations\":[\(conversationJSON)]}"
        } else if request.httpMethod == "POST", path.hasSuffix("/messages") {
            guard let body = try JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: String],
                  let clientID = body["client_message_id"], let content = body["content"] else {
                throw URLError(.cannotParseResponse)
            }
            let encodedContent = String(decoding: try JSONEncoder().encode(content), as: UTF8.self)
            json = """
            {"message":{"id":"30000000-0000-4000-8000-000000000003","conversation_id":"\(testConversationID)",
            "role":"user","content":\(encodedContent),
            "created_at":"2026-10-27T00:00:01Z","status":"completed","client_message_id":"\(clientID)","run_id":null},
            "run_id":"\(testRunID)","status":"accepted"}
            """
        } else if path.hasSuffix("/messages") {
            if let messageErrorCode { throw URLError(messageErrorCode) }
            json = "{\"messages\":[]}"
        } else if path == "/v1/events" {
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            let after = Int64(items.first { $0.name == "after" }!.value!)!
            cursors.append(after)
            let events = missedEvent.map { $0.seq > after ? [$0] : [] } ?? []
            return HTTPResult(data: try WireJSON.encoder().encode(EventsPage(events: events, nextCursor: events.last?.seq ?? after, hasMore: false)), status: 200)
        } else if request.httpMethod == "PATCH" {
            if rejectModel {
                return HTTPResult(data: Data("{\"error\":{\"code\":\"model_not_allowed\",\"message\":\"服务端拒绝此模型\"}}".utf8), status: 403)
            }
            json = "{\"conversation\":\(conversationJSON)}"
        } else { throw URLError(.unsupportedURL) }
        return HTTPResult(data: Data(json.utf8), status: 200)
    }
}
private actor UIStreams: SSETransport {
    private var connections: [SSEConnection]
    private(set) var requests: [URLRequest] = []
    init(_ connections: [SSEConnection]) { self.connections = connections }
    func open(_ request: URLRequest) async throws -> SSEConnection {
        requests.append(request)
        guard !connections.isEmpty else { throw URLError(.notConnectedToInternet) }
        return connections.removeFirst()
    }
}
private func fixtureConnection() throws -> PairedConnection {
    let json = """
    {"device":{"id":"50000000-0000-4000-8000-000000000005","name":"Fixture","platform":"macos",
    "permissions":["chat:read","chat:write"],"created_at":"2026-10-27T00:00:00Z","revoked_at":null},
    "token":"fixture-token","server_id":"\(serverID)","api_version":1}
    """
    return PairedConnection(endpoint: try ServiceEndpoint("https://example.com"),
        pairing: try WireJSON.decoder().decode(PairingResponse.self, from: Data(json.utf8)))
}
private func delta(_ seq: Int64, _ content: String) -> ServerEvent {
    ServerEvent(seq: seq, type: "assistant.delta", conversationID: testConversationID,
        payload: EventPayload(runID: testRunID, delta: content), createdAt: Date())
}
private func bytes(_ event: ServerEvent) throws -> Data {
    let json = String(decoding: try WireJSON.encoder().encode(event), as: UTF8.self)
    return Data("id: \(event.seq)\nevent: \(event.type)\ndata: \(json)\n\n".utf8)
}
@MainActor
private func waitUntil(_ check: () async -> Bool) async throws {
    let deadline = ContinuousClock.now.advanced(by: .seconds(3))
    while !(await check()) {
        if ContinuousClock.now > deadline { throw URLError(.timedOut) }
        try await Task.sleep(for: .milliseconds(10))
    }
}

@Test @MainActor func modelConsumesLiveDeltasBackfillsCursorAndCancelsInBackground() async throws {
    let (firstChunks, first) = AsyncThrowingStream<Data, Error>.makeStream()
    let (secondChunks, second) = AsyncThrowingStream<Data, Error>.makeStream()
    let cancelled = UIFLAG()
    let streams = UIStreams([
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: firstChunks, cancel: { first.finish() }),
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: secondChunks, cancel: { cancelled.set(); second.finish() }),
    ])
    let http = UIHTTP()
    let repository = ClientRepository(store: UIStore())
    let connection = try fixtureConnection(); try repository.saveConnection(connection)
    let model = ChatModel(repository: repository, transport: http, streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }
    first.yield(try bytes(delta(2, "你")))
    try await waitUntil { model.partialReplies.first?.content == "你" }
    await http.setMissed(delta(3, "好"))
    first.finish()
    try await waitUntil {
        let requests = await streams.requests
        return model.state.cursor == 3 && requests.count == 2
    }
    second.yield(try bytes(delta(3, "好")))
    try await Task.sleep(for: .milliseconds(30))
    #expect(model.partialReplies.first?.content == "你好")
    #expect(try repository.state(for: connection).cursor == 3)
    let requests = await streams.requests
    #expect(requests[1].value(forHTTPHeaderField: "Last-Event-ID") == "3")
    #expect(await http.cursors.contains(2))
    model.setActive(false)
    try await waitUntil { cancelled.isSet }
    #expect(model.connectionState == .disconnected)
}

@Test @MainActor func acceptedMessageExposesLoadingUntilTheFirstDelta() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() }),
    ])
    let repository = ClientRepository(store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = ChatModel(repository: repository, transport: UIHTTP(), streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }

    #expect(await model.send("hello"))
    let waiting = try #require(model.awaitingReplies.first)
    #expect(waiting.id == testRunID)
    #expect(AwaitingReplyPresentation(reply: waiting, isOnline: true).status == "正在思考…")
    #expect(AwaitingReplyPresentation(reply: waiting, isOnline: true).showsProgress)
    #expect(AwaitingReplyPresentation(reply: waiting, isOnline: false).status == "等待重新同步…")
    #expect(!AwaitingReplyPresentation(reply: waiting, isOnline: false).showsProgress)
    #expect(AwaitingReplyPresentation(reply: waiting, isOnline: true).accessibilityIdentifier ==
            "chat.awaiting-reply.\(testRunID.uuidString.lowercased())")

    continuation.yield(try bytes(delta(2, "你")))
    try await waitUntil { model.partialReplies.first?.content == "你" }
    #expect(model.awaitingReplies.isEmpty)
}

@Test @MainActor func catalogServiceErrorIsVisibleWithoutDisablingChat() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() })])
    let repository = ClientRepository(store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = ChatModel(repository: repository, transport: UIHTTP(catalogError: true), streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }
    #expect(model.modelCatalogError == "服务端登录需要处理")
}

@Test @MainActor func rejectedModelSelectionKeepsServerModelAndShowsError() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() })])
    let repository = ClientRepository(store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = ChatModel(repository: repository, transport: UIHTTP(rejectModel: true), streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }
    #expect(model.selectedModelName == "Model A")
    await model.changeModel(.model("model-b"))
    #expect(model.selectedConversation?.model == "model-a")
    #expect(model.errorMessage?.contains("服务端拒绝此模型") == true)
    #expect(model.isOnline)
}

@Test @MainActor func cancelledConversationLoadDoesNotShowAnErrorAlert() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() })])
    let http = UIHTTP()
    let repository = ClientRepository(store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = ChatModel(repository: repository, transport: http, streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }
    await http.setMessageError(.cancelled)
    await model.loadSelectedMessages()
    #expect(model.errorMessage == nil)
    #expect(model.isOnline)
}

@Test @MainActor func failedConversationLoadStillShowsTheRealError() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() })])
    let http = UIHTTP()
    let repository = ClientRepository(store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = ChatModel(repository: repository, transport: http, streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }
    await http.setMessageError(.notConnectedToInternet)
    await model.loadSelectedMessages()
    #expect(model.errorMessage != nil)
    #expect(model.errorMessage != URLError(.cancelled).localizedDescription)
}

@Test @MainActor func inactiveStartupRestoresHistoryWithoutOpeningAStream() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() })])
    let repository = ClientRepository(store: UIStore())
    let connection = try fixtureConnection(); try repository.saveConnection(connection)
    var state = ClientState()
    state.upsertConversation(Conversation(id: testConversationID, title: "Cached", createdAt: Date(), updatedAt: Date(), model: "model-a"))
    try repository.saveState(state, for: connection)
    let model = ChatModel(repository: repository, transport: UIHTTP(), streamTransport: streams)
    defer { model.setActive(false) }
    model.setActive(false)
    await model.start()
    #expect(model.selectedConversation?.title == "Cached")
    #expect(await streams.requests.isEmpty)
    model.setActive(true)
    try await waitUntil { model.isOnline }
    #expect(await streams.requests.count == 1)
}
