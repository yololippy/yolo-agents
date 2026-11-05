import Foundation
import SwiftUI
import Testing
@testable import YoloAgentsSDK
@testable import YoloAgentsUI

private let testConversationID = UUID(uuidString: "10000000-0000-4000-8000-000000000001")!
private let testRunID = UUID(uuidString: "20000000-0000-4000-8000-000000000002")!
private let serverID = "40000000-0000-4000-8000-000000000004"
private let chatTestIdentity = AgentAppIdentity(
    agentID: "fixture-agent",
    appName: "Fixture Agent",
    keychainService: "fixture.chat-model",
    defaultServiceAddress: "https://fixture.invalid"
)
private let conversationJSON = """
{"id":"\(testConversationID)","title":"Fixture","model":"model-a","archived":false,
"created_at":"2026-10-27T00:00:00Z","updated_at":"2026-10-27T00:00:00Z"}
"""

@Test func agentActionDockItemUsesAnExplicitOrFullCompactTitle() {
    let explicit = AgentActionDockItem(
        id: "orders",
        title: "Paper 订单",
        compactTitle: "订单",
        systemImage: "list.bullet.rectangle"
    )
    let fallback = AgentActionDockItem(
        id: "settings",
        title: "连接设置",
        systemImage: "gearshape"
    )

    #expect(explicit.compactTitle == "订单")
    #expect(fallback.compactTitle == "连接设置")
    #expect(explicit.accessibilityIdentifier == "orders")
}

@Test @MainActor func chatFeatureCarriesProductOwnedCompactCopy() {
    let feature = ChatFeature(
        id: "fixture.feature",
        title: "完整功能名称",
        compactTitle: "功能",
        systemImage: "star"
    ) { _ in
        EmptyView()
    }

    #expect(feature.compactTitle == "功能")
}

@Test @MainActor func chatModelUsesTheRequiredProductIdentity() throws {
    let model = try ChatModel(identity: chatTestIdentity,
                              repository: ClientRepository(identity: chatTestIdentity, store: UIStore()))

    #expect(model.identity == chatTestIdentity)
    #expect(model.appName == "Fixture Agent")
    #expect(model.defaultServiceAddress == "https://fixture.invalid")
}

@Test @MainActor func chatModelRejectsARepositoryOwnedByAnotherProduct() {
    let otherIdentity = AgentAppIdentity(
        agentID: "other-agent",
        appName: "Other Agent",
        keychainService: "fixture.other-agent",
        defaultServiceAddress: "https://other.invalid"
    )
    let repository = ClientRepository(identity: otherIdentity, store: UIStore())

    #expect(throws: ChatModelConfigurationError.repositoryIdentityMismatch) {
        _ = try ChatModel(identity: chatTestIdentity, repository: repository)
    }
}

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

@Test func serviceStatusesArePresentedIndependently() {
    #expect(ServiceStatusPresentation(ConnectionState.online) ==
            ServiceStatusPresentation(title: "Mac 服务", value: "已连接", tone: .positive,
                                      accessibilityIdentifier: "status.mac-service"))
    #expect(ServiceStatusPresentation(ModelConnectionState.recovering) ==
            ServiceStatusPresentation(title: "AI 模型", value: "待重试", tone: .warning,
                                      accessibilityIdentifier: "status.ai-model"))
    let ibkr = ServiceComponentHealth(id: "ibkr", label: "IBKR Paper",
                                      state: "unavailable", detail: "未连接")
    #expect(ServiceStatusPresentation(ibkr) ==
            ServiceStatusPresentation(title: "IBKR Paper", value: "未连接", tone: .negative,
                                      accessibilityIdentifier: "status.component.ibkr"))
}

@Test func conversationHomeKeepsPrimaryFirstAndSeparatesArchivedItems() {
    let primaryID = UUID(uuidString: "10000000-0000-4000-8000-000000000010")!
    let recentID = UUID(uuidString: "10000000-0000-4000-8000-000000000011")!
    let archivedID = UUID(uuidString: "10000000-0000-4000-8000-000000000012")!
    let now = Date()
    var state = ClientState()
    state.upsertConversation(Conversation(id: recentID, title: "最近任务", createdAt: now, updatedAt: now))
    state.upsertConversation(Conversation(id: primaryID, title: "main", createdAt: now,
                                          updatedAt: now.addingTimeInterval(-100)))
    state.upsertConversation(Conversation(id: archivedID, title: "旧任务", createdAt: now,
                                          updatedAt: now.addingTimeInterval(100), archived: true))
    state.setPrimaryConversation(primaryID)

    let visible = ConversationHomeSections(state: state, includeArchived: false)
    #expect(visible.primary.map(\.id) == [primaryID])
    #expect(visible.recent.map(\.id) == [recentID])
    #expect(visible.archived.isEmpty)

    let withArchive = ConversationHomeSections(state: state, includeArchived: true)
    #expect(withArchive.primary.map(\.id) == [primaryID])
    #expect(withArchive.recent.map(\.id) == [recentID])
    #expect(withArchive.archived.map(\.id) == [archivedID])
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
    private(set) var uploadedMediaIDs: [String] = []
    private(set) var submittedMediaIDs: [String] = []
    init(catalogError: Bool = false, rejectModel: Bool = false) {
        self.catalogError = catalogError; self.rejectModel = rejectModel
    }
    func setMissed(_ event: ServerEvent) { missedEvent = event }
    func setMessageError(_ code: URLError.Code?) { messageErrorCode = code }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        let path = request.url!.path
        let json: String
        if path == "/v1/health" {
            json = "{\"status\":\"ok\",\"api_version\":1,\"server_id\":\"\(serverID)\",\"agent_mode\":\"codex\",\"agent_id\":\"fixture-agent\",\"app_name\":\"Fixture Agent\",\"model\":null,\"trade_execution_enabled\":false}"
        } else if path == "/v1/models" {
            json = catalogError ? "{\"models\":[],\"default_model\":null,\"error\":{\"code\":\"codex_unavailable\",\"message\":\"服务端登录需要处理\"}}" :
                "{\"models\":[{\"id\":\"model-a\",\"display_name\":\"Model A\"},{\"id\":\"model-b\",\"display_name\":\"Model B\"}],\"default_model\":\"model-a\",\"auth\":{\"status\":\"authenticated\"}}"
        } else if path == "/v1/conversations" {
            json = "{\"conversations\":[\(conversationJSON)],\"primary_conversation_id\":\"\(testConversationID)\"}"
        } else if request.httpMethod == "POST", path == "/v1/media" {
            let id = request.value(forHTTPHeaderField: "X-Client-Media-ID") ?? ""
            uploadedMediaIDs.append(id)
            json = """
            {"media":{"id":"\(id)","kind":"image","media_type":"image/jpeg","byte_count":\(request.httpBody?.count ?? 0),
            "sha256":"\(String(repeating: "a", count: 64))","filename":"image.jpg","created_at":"2026-10-27T00:00:00Z"}}
            """
        } else if request.httpMethod == "POST", path.hasSuffix("/messages") {
            guard let body = try JSONSerialization.jsonObject(with: request.httpBody ?? Data()) as? [String: Any],
                  let clientID = body["client_message_id"] as? String,
                  let content = body["content"] as? String else {
                throw URLError(.cannotParseResponse)
            }
            let mediaIDs = body["media_ids"] as? [String] ?? []
            submittedMediaIDs = mediaIDs
            let mediaJSON = mediaIDs.map {
                "{\"id\":\"\($0)\",\"kind\":\"image\",\"media_type\":\"image/jpeg\",\"byte_count\":4,\"sha256\":\"\(String(repeating: "a", count: 64))\",\"filename\":\"image.jpg\",\"created_at\":\"2026-10-27T00:00:00Z\"}"
            }.joined(separator: ",")
            let encodedContent = String(decoding: try JSONEncoder().encode(content), as: UTF8.self)
            json = """
            {"message":{"id":"30000000-0000-4000-8000-000000000003","conversation_id":"\(testConversationID)",
            "role":"user","content":\(encodedContent),
            "created_at":"2026-10-27T00:00:01Z","status":"completed","client_message_id":"\(clientID)","run_id":null,
            "media":[\(mediaJSON)]},
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

private actor PagingUIHTTP: HTTPTransport {
    let latest: Message
    let older: Message
    private(set) var messageCursors: [Int64?] = []

    init(latest: Message, older: Message) {
        self.latest = latest; self.older = older
    }

    func send(_ request: URLRequest) async throws -> HTTPResult {
        let path = request.url!.path
        if path == "/v1/health" {
            return HTTPResult(data: Data("""
            {"status":"ok","api_version":1,"server_id":"\(serverID)","agent_mode":"codex",
            "agent_id":"fixture-agent","app_name":"Fixture Agent","model":null,"trade_execution_enabled":false}
            """.utf8), status: 200)
        }
        if path == "/v1/models" {
            return HTTPResult(data: Data("""
            {"models":[{"id":"model-a","display_name":"Model A"}],"default_model":"model-a",
            "auth":{"status":"authenticated"}}
            """.utf8), status: 200)
        }
        if path == "/v1/conversations" {
            return HTTPResult(data: Data("""
            {"conversations":[\(conversationJSON)],"primary_conversation_id":"\(testConversationID)"}
            """.utf8), status: 200)
        }
        if path.hasSuffix("/messages") {
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let cursor = items.first(where: { $0.name == "before" })?.value.flatMap(Int64.init)
            messageCursors.append(cursor)
            let page = cursor == nil
                ? MessagesPage(messages: [latest], nextCursor: 51, hasMore: true)
                : MessagesPage(messages: [older], nextCursor: nil, hasMore: false)
            return try encodedUI(page)
        }
        if path == "/v1/events" {
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let cursor = items.first(where: { $0.name == "after" })?.value.flatMap(Int64.init) ?? 0
            return try encodedUI(EventsPage(events: [], nextCursor: cursor, hasMore: false))
        }
        throw URLError(.unsupportedURL)
    }

    private func encodedUI<T: Encodable>(_ value: T) throws -> HTTPResult {
        HTTPResult(data: try WireJSON.encoder().encode(value), status: 200)
    }
}
private func fixtureConnection() throws -> PairedConnection {
    let json = """
    {"device":{"id":"50000000-0000-4000-8000-000000000005","name":"Fixture","platform":"macos",
    "permissions":["chat:read","chat:write"],"created_at":"2026-10-27T00:00:00Z","revoked_at":null},
    "token":"fixture-token","server_id":"\(serverID)","api_version":1}
    """
    return PairedConnection(endpoint: try ServiceEndpoint("https://example.com"),
        pairing: try WireJSON.decoder().decode(PairingResponse.self, from: Data(json.utf8)),
        agentID: chatTestIdentity.agentID)
}
private func delta(_ seq: Int64, _ content: String) -> ServerEvent {
    ServerEvent(seq: seq, type: "assistant.delta", conversationID: testConversationID,
        payload: EventPayload(runID: testRunID, delta: content), createdAt: Date())
}
private func activityMessage(
    id: UUID = UUID(), conversationID: UUID = testConversationID,
    role: MessageRole, status: MessageStatus = .completed,
    clientMessageID: UUID? = nil, runID: UUID? = nil
) -> Message {
    Message(id: id, conversationID: conversationID, role: role,
            content: role == .user ? "question" : "answer",
            createdAt: Date(), status: status,
            clientMessageID: clientMessageID, runID: runID)
}
private func activityEvent(
    _ seq: Int64, _ type: String, conversationID: UUID = testConversationID,
    payload: EventPayload
) -> ServerEvent {
    ServerEvent(seq: seq, type: type, conversationID: conversationID,
                payload: payload, createdAt: Date())
}
private func bytes(_ event: ServerEvent) throws -> Data {
    let json = String(decoding: try WireJSON.encoder().encode(event), as: UTF8.self)
    return Data("id: \(event.seq)\nevent: \(event.type)\ndata: \(json)\n\n".utf8)
}

@Test func conversationScrollFollowsLatestUntilTheUserScrollsUp() {
    let otherConversationID = UUID()
    let olderMessageID = UUID()
    var scroll = ConversationScrollState()

    scroll.open(testConversationID)
    #expect(scroll.target == .bottom(testConversationID))
    #expect(scroll.followsLatest)

    scroll.positionChanged(.message(olderMessageID), in: testConversationID)
    scroll.contentChanged(in: testConversationID)
    #expect(scroll.target == .message(olderMessageID))
    #expect(!scroll.followsLatest)

    scroll.positionChanged(.bottom(testConversationID), in: testConversationID)
    scroll.contentChanged(in: testConversationID)
    #expect(scroll.target == .bottom(testConversationID))
    #expect(scroll.followsLatest)

    scroll.open(otherConversationID)
    scroll.positionChanged(.message(olderMessageID), in: testConversationID)
    #expect(scroll.target == .bottom(otherConversationID))
    #expect(scroll.conversationID == otherConversationID)
}

@Test func conversationProgressTracksRemoteRunsAndTerminalCatchUp() throws {
    let secondRunID = UUID()
    var state = ClientState()
    state = try state.applying(activityEvent(
        1, "message.created",
        payload: EventPayload(message: activityMessage(role: .user, runID: testRunID))))
    state = try state.applying(activityEvent(
        2, "message.created",
        payload: EventPayload(message: activityMessage(role: .user, runID: secondRunID))))

    #expect(state.activeRunIDs(in: testConversationID) == [testRunID, secondRunID])
    #expect(ConversationActivity(state: state, conversationID: testConversationID).showsProgress)

    let completed = activityMessage(role: .assistant, runID: testRunID)
    state = try state.applying(activityEvent(
        3, "assistant.completed",
        payload: EventPayload(message: completed, runID: testRunID)))
    #expect(state.activeRunIDs(in: testConversationID) == [secondRunID])

    let failed = activityMessage(role: .assistant, status: .failed, runID: secondRunID)
    state = try state.applying(activityEvent(
        4, "assistant.failed",
        payload: EventPayload(message: failed, runID: secondRunID,
                              error: ServiceError(code: "failed", message: "failed"))))
    #expect(!ConversationActivity(state: state, conversationID: testConversationID).showsProgress)
}

@Test func conversationProgressHandlesAcceptedPartialAndFailedRuns() throws {
    let clientMessageID = UUID()
    var state = ClientState()
    state.enqueue(content: "question", conversationID: testConversationID,
                  id: clientMessageID, now: Date())
    let canonical = activityMessage(role: .user, clientMessageID: clientMessageID)
    try state.accept(
        SendMessageResponse(message: canonical, runID: testRunID, status: "accepted"),
        clientMessageID: clientMessageID)
    #expect(state.isRunning(conversationID: testConversationID))

    state = try state.applying(delta(1, "partial"))
    #expect(state.isRunning(conversationID: testConversationID))
    #expect(state.activeRunIDs(in: testConversationID) == [testRunID])

    state = try state.applying(activityEvent(
        2, "assistant.failed",
        payload: EventPayload(runID: testRunID,
                              error: ServiceError(code: "timeout", message: "timeout"))))
    #expect(!state.isRunning(conversationID: testConversationID))
}

@Test func terminalBeforeAcceptanceAndCacheRestoreDoNotReviveFinishedRuns() throws {
    let store = UIStore()
    let repository = ClientRepository(identity: chatTestIdentity, store: store)
    let connection = try fixtureConnection()
    try repository.saveConnection(connection)

    let finishedClientID = UUID()
    var state = ClientState()
    state.enqueue(content: "finished", conversationID: testConversationID,
                  id: finishedClientID, now: Date())
    let completed = activityMessage(role: .assistant, runID: testRunID)
    state = try state.applying(activityEvent(
        1, "assistant.completed",
        payload: EventPayload(message: completed, runID: testRunID)))
    try state.accept(
        SendMessageResponse(
            message: activityMessage(role: .user, clientMessageID: finishedClientID),
            runID: testRunID, status: "duplicate"),
        clientMessageID: finishedClientID)
    #expect(!state.isRunning(conversationID: testConversationID))

    let activeRunID = UUID()
    let activeClientID = UUID()
    state.enqueue(content: "active", conversationID: testConversationID,
                  id: activeClientID, now: Date())
    try state.accept(
        SendMessageResponse(
            message: activityMessage(role: .user, clientMessageID: activeClientID),
            runID: activeRunID, status: "accepted"),
        clientMessageID: activeClientID)
    let retryOnlyConversationID = UUID()
    state.enqueue(content: "unconfirmed", conversationID: retryOnlyConversationID,
                  id: UUID(), now: Date())
    try repository.saveState(state, for: connection)

    let restored = try repository.state(for: connection)
    #expect(restored.activeRunIDs(in: testConversationID) == [activeRunID])
    #expect(restored.outgoing.contains { outgoing in
        outgoing.conversationID == retryOnlyConversationID && outgoing.status == .needsRetry
    })
    #expect(!restored.isRunning(conversationID: retryOnlyConversationID))
}

@MainActor
private func waitUntil(_ check: () async -> Bool) async throws {
    let deadline = ContinuousClock.now.advanced(by: .seconds(3))
    while !(await check()) {
        if ContinuousClock.now > deadline { throw URLError(.timedOut) }
        try await Task.sleep(for: .milliseconds(10))
    }
}

@Test @MainActor func modelBackfillsAndSilentlyResumesAfterBackgrounding() async throws {
    let (firstChunks, first) = AsyncThrowingStream<Data, Error>.makeStream()
    let (secondChunks, second) = AsyncThrowingStream<Data, Error>.makeStream()
    let (resumedChunks, resumed) = AsyncThrowingStream<Data, Error>.makeStream()
    let cancelled = UIFLAG()
    let streams = UIStreams([
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: firstChunks, cancel: { first.finish() }),
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: secondChunks, cancel: { cancelled.set(); second.finish() }),
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: resumedChunks, cancel: { resumed.finish() }),
    ])
    let http = UIHTTP()
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore())
    let connection = try fixtureConnection(); try repository.saveConnection(connection)
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: http, streamTransport: streams)
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
    #expect(model.connectionState == .online)
    model.setActive(true)
    try await waitUntil { await streams.requests.count == 3 }
    #expect(model.connectionState == .online)
    #expect((await streams.requests)[2].value(forHTTPHeaderField: "Last-Event-ID") == "3")
}

@Test @MainActor func acceptedMessageExposesLoadingUntilTheFirstDelta() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() }),
    ])
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: UIHTTP(), streamTransport: streams)
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

@Test @MainActor func imageOnlyMessageUploadsBeforeSubmittingAndKeepsCanonicalMedia() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks,
                      cancel: { continuation.finish() }),
    ])
    let http = UIHTTP()
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: http, streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }
    let mediaID = UUID(uuidString: "60000000-0000-4000-8000-000000000006")!
    let image = DraftImage(id: mediaID, data: Data([0xff, 0xd8, 0xff, 0xd9]),
                           mediaType: "image/jpeg", filename: "image.jpg")

    #expect(await model.send("", images: [image]))
    #expect(await http.uploadedMediaIDs == [mediaID.uuidString])
    #expect(await http.submittedMediaIDs == [mediaID.uuidString])
    #expect(model.messages.first?.content == "")
    #expect(model.messages.first?.media.map(\.id) == [mediaID])
}

@Test @MainActor func catalogServiceErrorIsVisibleWithoutDisablingChat() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() })])
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: UIHTTP(catalogError: true), streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }
    #expect(model.modelCatalogError == "服务端登录需要处理")
}

@Test @MainActor func rejectedModelSelectionKeepsServerModelAndShowsError() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() })])
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: UIHTTP(rejectModel: true), streamTransport: streams)
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
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: http, streamTransport: streams)
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
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: http, streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline }
    await http.setMessageError(.notConnectedToInternet)
    await model.loadSelectedMessages()
    #expect(model.errorMessage != nil)
    #expect(model.errorMessage != URLError(.cancelled).localizedDescription)
}

@Test @MainActor func conversationLoadsLatestPageThenFetchesOlderMessages() async throws {
    let latest = Message(
        id: UUID(), conversationID: testConversationID, role: .assistant, content: "latest",
        createdAt: Date(timeIntervalSince1970: 2), status: .completed
    )
    let older = Message(
        id: UUID(), conversationID: testConversationID, role: .user, content: "older",
        createdAt: Date(timeIntervalSince1970: 1), status: .completed
    )
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([
        SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks,
                      cancel: { continuation.finish() }),
    ])
    let http = PagingUIHTTP(latest: latest, older: older)
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore()); try repository.saveConnection(fixtureConnection())
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: http, streamTransport: streams)
    defer { model.setActive(false) }
    await model.start()
    try await waitUntil { model.isOnline && model.messages == [latest] }
    #expect(model.canLoadEarlierMessages)
    #expect(model.earlierMessagesCursor == 51)

    await model.loadEarlierMessages()
    #expect(model.messages == [older, latest])
    #expect(!model.canLoadEarlierMessages)
    #expect(await http.messageCursors == [nil, 51])
}

@Test @MainActor func inactiveStartupRestoresHistoryWithoutOpeningAStream() async throws {
    let (chunks, continuation) = AsyncThrowingStream<Data, Error>.makeStream()
    let streams = UIStreams([SSEConnection(status: 200, contentType: "text/event-stream", chunks: chunks, cancel: { continuation.finish() })])
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore())
    let connection = try fixtureConnection(); try repository.saveConnection(connection)
    var state = ClientState()
    state.upsertConversation(Conversation(id: testConversationID, title: "Cached", createdAt: Date(), updatedAt: Date(), model: "model-a"))
    try repository.saveState(state, for: connection)
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: UIHTTP(), streamTransport: streams)
    defer { model.setActive(false) }
    model.setActive(false)
    await model.start()
    #expect(model.selectedConversation?.title == "Cached")
    #expect(await streams.requests.isEmpty)
    model.setActive(true)
    try await waitUntil { model.isOnline }
    #expect(await streams.requests.count == 1)
}

@Test @MainActor func coldStartSelectsCachedPrimaryInsteadOfNewerTaskConversation() async throws {
    let taskID = UUID(uuidString: "10000000-0000-4000-8000-000000000099")!
    let streams = UIStreams([])
    let repository = ClientRepository(identity: chatTestIdentity, store: UIStore())
    let connection = try fixtureConnection(); try repository.saveConnection(connection)
    var state = ClientState()
    state.upsertConversation(Conversation(
        id: testConversationID, title: "主会话", createdAt: Date(timeIntervalSince1970: 1),
        updatedAt: Date(timeIntervalSince1970: 1), model: "model-a"))
    state.upsertConversation(Conversation(
        id: taskID, title: "较新的任务", createdAt: Date(timeIntervalSince1970: 2),
        updatedAt: Date(timeIntervalSince1970: 2), model: "model-a"))
    state.setPrimaryConversation(testConversationID)
    try repository.saveState(state, for: connection)
    let model = try ChatModel(identity: chatTestIdentity, repository: repository,
                          transport: UIHTTP(), streamTransport: streams)
    model.setActive(false)
    await model.start()
    #expect(model.selectedID == testConversationID)
    #expect(model.selectedConversation?.title == "主会话")
    #expect(await streams.requests.isEmpty)
}
