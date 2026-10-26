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
    @Published public private(set) var busy = false
    @Published public var selectedID: UUID?
    @Published public var errorMessage: String?
    private let repository: ClientRepository
    private var polling: Task<Void, Never>?
    private var generation = UUID()
    private var active = true

    public init(repository: ClientRepository = ClientRepository()) { self.repository = repository }
    public var isOnline: Bool { connectionState == .online }
    public var selectedConversation: Conversation? { state.conversations.first { $0.id == selectedID } }
    public var messages: [Message] { selectedID.map { state.messages(in: $0) } ?? [] }
    public var partialReplies: [PartialReply] {
        state.partialReplies.filter { $0.conversationID == selectedID }.sorted { $0.createdAt < $1.createdAt }
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
            connection = try repository.connection()
            if let connection {
                guard connection.pairing.apiVersion == 1, connection.pairing.device.revokedAt == nil else {
                    connectionState = .authorizationRequired; return
                }
                state = try repository.state(for: connection)
                selectedID = state.conversations.first(where: { !$0.archived })?.id
                startPolling()
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
            let client = APIClient(endpoint: endpoint)
            let health = try await client.health()
            guard health.apiVersion == 1 else { throw ClientError.incompatibleServer }
            let response = try await client.pair(code: code.trimmingCharacters(in: .whitespacesAndNewlines),
                name: deviceName.trimmingCharacters(in: .whitespacesAndNewlines), platform: devicePlatform)
            guard generation == attempt else { return false }
            guard response.apiVersion == 1, response.serverID == health.serverID,
                  response.device.revokedAt == nil, !response.token.isEmpty else { throw ClientError.incompatibleServer }
            let connection = PairedConnection(endpoint: endpoint, pairing: response)
            try repository.saveState(ClientState(), for: connection)
            try repository.saveConnection(connection)
            polling?.cancel(); polling = nil
            generation = UUID()
            self.connection = connection; self.health = health
            state = ClientState(); selectedID = nil
            connectionState = .disconnected
            errorMessage = nil
            startPolling()
            return true
        } catch {
            if generation == attempt { errorMessage = error.localizedDescription }
            return false
        }
    }
    public func setActive(_ value: Bool) {
        active = value
        if value { startPolling() } else {
            polling?.cancel(); polling = nil
            if connectionState == .online || connectionState == .connecting { connectionState = .disconnected }
        }
    }
    public func reconnect() {
        guard connectionState != .authorizationRequired else { return }
        polling?.cancel(); polling = nil
        // Invalidate every in-flight operation before making a replacement connection.
        generation = UUID()
        var next = state
        next.prepareForLaunch()
        do { try commit(next) } catch { errorMessage = error.localizedDescription }
        startPolling()
    }
    private func startPolling() {
        guard active, connection != nil, polling == nil, connectionState != .authorizationRequired else { return }
        connectionState = .connecting
        let attempt = generation
        polling = Task { [weak self] in
            var needsSnapshot = true
            var retrySeconds: UInt64 = 2
            while !Task.isCancelled {
                guard let self, self.generation == attempt else { return }
                do {
                    try await self.synchronize(refresh: needsSnapshot, attempt: attempt)
                    try Task.checkCancellation()
                    guard self.generation == attempt else { return }
                    self.connectionState = .online
                    needsSnapshot = false; retrySeconds = 2
                } catch {
                    guard !Task.isCancelled, self.generation == attempt else { return }
                    if let error = error as? ClientError, error.requiresPairing {
                        self.connectionState = .authorizationRequired
                        self.polling = nil
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
        let api = APIClient(endpoint: connection.endpoint, token: connection.pairing.token)
        if refresh {
            let health = try await api.health()
            try check(attempt)
            guard health.apiVersion == 1, health.serverID == connection.pairing.serverID else {
                throw ClientError.incompatibleServer
            }
            self.health = health
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
    private func commit(_ next: ClientState) throws {
        guard let connection, next != state else { return }
        try repository.saveState(next, for: connection)
        state = next
    }
    public func loadSelectedMessages() async {
        guard let id = selectedID, let connection, isOnline else { return }
        let attempt = generation
        do {
            let messages = try await APIClient(endpoint: connection.endpoint, token: connection.pairing.token)
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
            let conversation = try await APIClient(endpoint: connection.endpoint, token: connection.pairing.token)
                .createConversation(title: "新会话")
            try check(attempt)
            var next = state; next.upsertConversation(conversation)
            try commit(next)
            selectedID = conversation.id
        } catch { handle(error, attempt: attempt) }
    }
    public func updateConversation(_ conversation: Conversation, title: String? = nil, archived: Bool? = nil) async {
        guard isOnline, let connection else { return }
        let attempt = generation
        do {
            let updated = try await APIClient(endpoint: connection.endpoint, token: connection.pairing.token)
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
            let response = try await APIClient(endpoint: connection.endpoint, token: connection.pairing.token)
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
        guard generation == attempt, !(error is CancellationError) else { return }
        errorMessage = error.localizedDescription
        if let error = error as? ClientError, error.requiresPairing {
            polling?.cancel(); polling = nil
            connectionState = .authorizationRequired
        }
    }
    public func disconnect() {
        polling?.cancel(); polling = nil; generation = UUID()
        if let connection {
            do { try repository.forget(connection) }
            catch { errorMessage = error.localizedDescription }
        }
        connection = nil; state = ClientState(); selectedID = nil; health = nil
        connectionState = .disconnected
    }
}
