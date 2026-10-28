import Foundation
import SwiftUI
import YoloAgentsSDK

public enum ConnectionState: Equatable {
    case disconnected, connecting, online, offline(String), authorizationRequired
    public var label: String {
        switch self {
        case .disconnected: "未连接"
        case .connecting: "正在同步"
        case .online: "已连接"
        case .offline: "离线 · 只读历史"
        case .authorizationRequired: "需要重新配对"
        }
    }
}

@MainActor
public final class ChatModel: ObservableObject {
    @Published public private(set) var started = false
    @Published public private(set) var connection: PairedConnection?
    @Published public private(set) var state = ClientState()
    @Published public private(set) var connectionState: ConnectionState = .disconnected
    @Published public private(set) var health: Health?
    @Published public private(set) var modelCatalog: ModelCatalog?
    @Published public private(set) var modelCatalogError: String?
    @Published public private(set) var modelCatalogLoading = false
    @Published public private(set) var busy = false
    @Published public var selectedID: UUID?
    @Published public var errorMessage: String?
    private let repository: ClientRepository
    private let transport: any HTTPTransport
    private let streamTransport: any SSETransport
    private var synchronizationTask: Task<Void, Never>?
    private var generation = UUID()
    private var active = true
    public let identity: AgentAppIdentity?
    public var appName: String { identity?.appName ?? "Agent" }
    public var defaultServiceAddress: String { identity?.defaultServiceAddress ?? "http://127.0.0.1:8765" }
    public var legacyIdentityNotice: String? {
        guard identity?.allowsLegacyServer == true, let health, health.agentID == nil else { return nil }
        return "当前为旧服务兼容连接，服务未提供 Agent 身份。请更新服务端以启用完整身份校验。"
    }

    public init(identity: AgentAppIdentity? = nil, repository: ClientRepository? = nil,
                transport: any HTTPTransport = URLSessionTransport(),
                streamTransport: any SSETransport = URLSessionSSETransport()) {
        self.identity = identity
        self.repository = repository ?? identity.map { ClientRepository(identity: $0) } ?? ClientRepository()
        self.transport = transport; self.streamTransport = streamTransport
    }
    private func client(for connection: PairedConnection) -> APIClient {
        APIClient(endpoint: connection.endpoint, token: connection.pairing.token,
                  transport: transport, streamTransport: streamTransport)
    }
    public var isOnline: Bool { connectionState == .online }
    public var selectedConversation: Conversation? { state.conversations.first { $0.id == selectedID } }
    public var effectiveModel: String? { selectedConversation?.model ?? modelCatalog?.defaultModel ?? health?.model }
    public var selectedModelName: String {
        guard let id = effectiveModel else { return "默认模型" }
        return modelCatalog?.models.first(where: { $0.id == id })?.name ?? id
    }
    public var messages: [Message] { selectedID.map { state.messages(in: $0) } ?? [] }
    public var partialReplies: [PartialReply] {
        state.partialReplies.filter { $0.conversationID == selectedID }.sorted { $0.createdAt < $1.createdAt }
    }
    public var awaitingReplies: [AwaitingReply] {
        selectedID.map { state.awaitingReplies(in: $0) } ?? []
    }
    public var devicePlatform: DevicePlatform {
        #if os(iOS)
        .ios
        #else
        .macos
        #endif
    }
    public func start() async {
        guard !started else { return }
        defer { started = true }
        do {
            let repository = repository
            let restored = try await Task.detached(priority: .userInitiated) {
                let connection = try repository.connection()
                let state = try connection.map { try repository.state(for: $0) }
                return (connection, state)
            }.value
            connection = restored.0
            if let connection, let restoredState = restored.1 {
                guard connection.pairing.apiVersion == 1, connection.pairing.device.revokedAt == nil else {
                    connectionState = .authorizationRequired; return
                }
                state = restoredState
                selectedID = state.conversations.first(where: { !$0.archived })?.id
                startSynchronizing()
            }
        } catch { errorMessage = error.localizedDescription }
    }
    public func pair(address: String, code: String, deviceName: String, allowHTTP: Bool) async -> Bool {
        guard !busy else { return false }
        busy = true
        defer { busy = false }
        let attempt = generation
        do {
            let endpoint = try ServiceEndpoint(address, allowInsecureLocalHTTP: allowHTTP)
            let client = APIClient(endpoint: endpoint, transport: transport, streamTransport: streamTransport)
            let health = try await client.health()
            guard health.apiVersion == 1 else { throw ClientError.incompatibleServer }
            try identity?.validate(health)
            let response = try await client.pair(code: code.trimmingCharacters(in: .whitespacesAndNewlines),
                name: deviceName.trimmingCharacters(in: .whitespacesAndNewlines), platform: devicePlatform)
            guard generation == attempt else { return false }
            guard response.apiVersion == 1, response.serverID == health.serverID,
                  response.device.revokedAt == nil, !response.token.isEmpty else { throw ClientError.incompatibleServer }
            let connection = PairedConnection(endpoint: endpoint, pairing: response, agentID: health.agentID)
            try repository.saveState(ClientState(), for: connection)
            try repository.saveConnection(connection)
            synchronizationTask?.cancel(); synchronizationTask = nil
            generation = UUID()
            self.connection = connection; self.health = health
            modelCatalog = nil; modelCatalogError = nil; modelCatalogLoading = false
            state = ClientState(); selectedID = nil
            connectionState = .disconnected
            errorMessage = nil
            startSynchronizing()
            return true
        } catch {
            if generation == attempt { errorMessage = error.localizedDescription }
            return false
        }
    }
    public func setActive(_ value: Bool) {
        active = value
        if value { startSynchronizing() } else {
            synchronizationTask?.cancel(); synchronizationTask = nil
            if connectionState == .online || connectionState == .connecting { connectionState = .disconnected }
        }
    }
    public func reconnect() {
        guard connectionState != .authorizationRequired else { return }
        synchronizationTask?.cancel(); synchronizationTask = nil
        // Invalidate every in-flight operation before making a replacement connection.
        generation = UUID()
        var next = state
        next.prepareForLaunch()
        do { try commit(next) } catch { errorMessage = error.localizedDescription }
        startSynchronizing()
    }
    private func startSynchronizing() {
        guard active, connection != nil, synchronizationTask == nil, connectionState != .authorizationRequired else { return }
        connectionState = .connecting
        let attempt = generation
        synchronizationTask = Task { [weak self] in
            var needsSnapshot = true
            var retrySeconds: UInt64 = 2
            while !Task.isCancelled {
                guard let self, self.generation == attempt else { return }
                do {
                    try await self.synchronize(refresh: needsSnapshot, attempt: attempt)
                    try Task.checkCancellation()
                    guard self.generation == attempt else { return }
                    guard let connection = self.connection else { return }
                    let stream = try await self.client(for: connection).eventStream(after: self.state.cursor)
                    defer { stream.cancel() }
                    try self.check(attempt)
                    self.connectionState = .online
                    needsSnapshot = false; retrySeconds = 2
                    let openedAt = ContinuousClock.now
                    var receivedEvent = false
                    for try await event in stream {
                        try self.check(attempt)
                        try self.commit(self.state.applying(event))
                        receivedEvent = true
                    }
                    try self.check(attempt)
                    // Normal server rotation reconnects immediately. An endpoint that
                    // closes without events must not cause an unbounded request loop.
                    if !receivedEvent && openedAt.duration(to: .now) < .seconds(1) {
                        throw URLError(.networkConnectionLost)
                    }
                    continue
                } catch {
                    guard !Task.isCancelled, self.generation == attempt else { return }
                    if let error = error as? ClientError, error.requiresPairing {
                        self.connectionState = .authorizationRequired
                        self.errorMessage = error.localizedDescription
                        self.synchronizationTask = nil
                        return
                    }
                    self.connectionState = .offline(error.localizedDescription)
                    needsSnapshot = true
                    retrySeconds = min(30, retrySeconds * 2)
                }
                do { try await Task.sleep(nanoseconds: retrySeconds * 1_000_000_000) }
                catch { return }
            }
        }
    }
    private func synchronize(refresh: Bool, attempt: UUID) async throws {
        guard let connection else { return }
        let api = client(for: connection)
        if refresh {
            let health = try await api.health()
            try check(attempt)
            guard health.apiVersion == 1, health.serverID == connection.pairing.serverID else {
                throw ClientError.incompatibleServer
            }
            try identity?.validate(health, serverID: connection.pairing.serverID)
            self.health = health
            await refreshModels()
            try check(attempt)
            let conversations = try await api.conversations(includeArchived: true)
            try check(attempt)
            var next = state
            conversations.forEach { next.upsertConversation($0) }
            try commit(next)
            if selectedID == nil { selectedID = state.conversations.first(where: { !$0.archived })?.id }
            if let selectedID {
                let messages = try await api.messages(conversationID: selectedID)
                try check(attempt)
                var next = state
                try next.mergeMessages(messages, conversationID: selectedID)
                try commit(next)
            }
        }
        while true {
            try check(attempt)
            let page = try await api.events(after: state.cursor)
            try check(attempt)
            try commit(state.applying(page))
            if !page.hasMore { break }
        }
    }
    private func check(_ attempt: UUID) throws {
        try Task.checkCancellation()
        if generation != attempt { throw CancellationError() }
    }
    private func isCancellation(_ error: Error) -> Bool {
        if Task.isCancelled || error is CancellationError { return true }
        if let urlError = error as? URLError, urlError.code == .cancelled { return true }
        let error = error as NSError
        return error.domain == NSURLErrorDomain && error.code == URLError.Code.cancelled.rawValue
    }
    private func commit(_ next: ClientState) throws {
        guard let connection, next != state else { return }
        try repository.saveState(next, for: connection)
        state = next
    }
    public func loadSelectedMessages() async {
        guard let id = selectedID, let connection, isOnline else { return }
        let attempt = generation
        do {
            let messages = try await client(for: connection)
                .messages(conversationID: id)
            try check(attempt)
            var next = state
            try next.mergeMessages(messages, conversationID: id)
            try commit(next)
        } catch { handle(error, attempt: attempt) }
    }
    public func createConversation() async {
        guard isOnline, !busy, let connection else { return }
        busy = true
        defer { busy = false }
        let attempt = generation
        do {
            let conversation = try await client(for: connection)
                .createConversation(title: "新会话")
            try check(attempt)
            var next = state; next.upsertConversation(conversation)
            try commit(next)
            selectedID = conversation.id
        } catch { handle(error, attempt: attempt) }
    }
    public func refreshModels() async {
        guard let connection, !modelCatalogLoading else { return }
        let attempt = generation
        modelCatalogLoading = true
        defer { if generation == attempt { modelCatalogLoading = false } }
        do {
            let catalog = try await client(for: connection).models()
            try check(attempt)
            modelCatalog = catalog
            modelCatalogError = catalog.error?.message
        } catch {
            guard generation == attempt, !Task.isCancelled else { return }
            modelCatalogError = error.localizedDescription
        }
    }
    public func changeModel(_ selection: ConversationModelSelection) async {
        guard isOnline, !busy, let connection, let conversation = selectedConversation, !conversation.archived else { return }
        busy = true
        defer { busy = false }
        let attempt = generation
        do {
            let updated = try await client(for: connection).updateConversation(conversation.id, model: selection)
            try check(attempt)
            var next = state; next.upsertConversation(updated); try commit(next)
        } catch {
            guard generation == attempt, !isCancellation(error) else { return }
            // Catalog selection is not proof of provider entitlement. Preserve the
            // current model and show the actual server error when selection fails.
            errorMessage = error.localizedDescription
            if case ClientError.http(let status, _, _) = error, status == 401 { handle(error, attempt: attempt) }
        }
    }
    public func updateConversation(_ conversation: Conversation, title: String? = nil, archived: Bool? = nil) async {
        guard isOnline, let connection else { return }
        let attempt = generation
        do {
            let updated = try await client(for: connection)
                .updateConversation(conversation.id, title: title, archived: archived)
            try check(attempt)
            var next = state; next.upsertConversation(updated); try commit(next)
            if archived == true, selectedID == conversation.id { selectedID = nil }
        } catch { handle(error, attempt: attempt) }
    }
    /// Persist the intent before the first request. A failed/unknown request can only
    /// be retried with the original UUID, content, and conversation.
    public func send(_ content: String) async -> Bool {
        guard isOnline, !busy, let id = selectedID,
              !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        guard state.outgoing.count < 20 else {
            errorMessage = "还有较多消息等待处理，请先核对发送状态。"; return false
        }
        do {
            try APIClient.validateMessageContent(content)
            var next = state
            let messageID = next.enqueue(content: content, conversationID: id)
            try commit(next)
            await deliver(messageID)
            return true
        } catch { errorMessage = error.localizedDescription; return false }
    }
    public func retry(_ id: UUID) async {
        guard isOnline, !busy, state.outgoing.contains(where: { $0.id == id && $0.status == .needsRetry }) else { return }
        do {
            var next = state; next.markSubmitting(id); try commit(next)
            await deliver(id)
        } catch { errorMessage = error.localizedDescription }
    }
    private func deliver(_ id: UUID) async {
        guard let connection, let outgoing = state.outgoing.first(where: { $0.id == id }) else { return }
        busy = true
        defer { busy = false }
        let attempt = generation
        do {
            let response = try await client(for: connection)
                .sendMessage(conversationID: outgoing.conversationID, clientMessageID: id, content: outgoing.content)
            try check(attempt)
            var next = state; try next.accept(response, clientMessageID: id); try commit(next)
        } catch {
            guard generation == attempt else { return }
            var next = state; next.markNeedsRetry(id, error: error.localizedDescription)
            do { try commit(next) } catch { errorMessage = error.localizedDescription }
            handle(error, attempt: attempt)
        }
    }
    private func handle(_ error: Error, attempt: UUID) {
        guard generation == attempt, !isCancellation(error) else { return }
        errorMessage = error.localizedDescription
        if let error = error as? ClientError, error.requiresPairing {
            synchronizationTask?.cancel(); synchronizationTask = nil
            connectionState = .authorizationRequired
        }
    }
    public func disconnect() {
        synchronizationTask?.cancel(); synchronizationTask = nil; generation = UUID()
        if let connection {
            do { try repository.forget(connection) }
            catch { errorMessage = error.localizedDescription }
        }
        connection = nil; state = ClientState(); selectedID = nil; health = nil
        modelCatalog = nil; modelCatalogError = nil; modelCatalogLoading = false
        connectionState = .disconnected
    }
}
