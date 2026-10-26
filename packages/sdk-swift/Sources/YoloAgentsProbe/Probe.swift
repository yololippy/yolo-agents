import Foundation
import Darwin
import YoloAgentsSDK

private struct ProbeFailure: Error { let reason: String }
private func verify(_ value: Bool, _ reason: String) throws {
    if !value { throw ProbeFailure(reason: reason) }
}

/// Requires only the compiler and SDK. Also works unchanged as an executableTarget
/// in an SDK-only temporary package; it never reads or writes the user's Keychain.
@main
struct YoloAgentsProbe {
    static func main() async {
        let environment = ProcessInfo.processInfo.environment
        guard let address = environment["YOLO_AGENTS_TEST_URL"], !address.isEmpty,
              let code = environment["YOLO_AGENTS_PAIRING_CODE"], !code.isEmpty else {
            print("SKIP: configure YOLO_AGENTS_TEST_URL and YOLO_AGENTS_PAIRING_CODE for a disposable demo service.")
            return
        }
        do {
            try await run(address: address, code: code)
            print("PASS: SDK paired, sent Chinese text, replayed events, deduplicated a retry, read history, and rejected a revoked device.")
        } catch let error as ProbeFailure {
            FileHandle.standardError.write(Data("FAIL: \(error.reason)\n".utf8))
            exit(1)
        } catch {
            // Do not dump requests, response objects, environment, codes or tokens.
            FileHandle.standardError.write(Data("FAIL: SDK service request or protocol decoding did not complete.\n".utf8))
            exit(1)
        }
    }

    private static func run(address: String, code: String) async throws {
        let endpoint = try ServiceEndpoint(address, allowInsecureLocalHTTP: true)
        let publicAPI = APIClient(endpoint: endpoint)
        let health = try await publicAPI.health()
        try verify(health.apiVersion == 1 && health.agentMode == "demo" && !health.tradeExecutionEnabled,
                   "Use a disposable v1 demo service with trading disabled.")
        let paired = try await publicAPI.pair(code: code, name: "Swift SDK disposable probe", platform: .macos)
        let api = APIClient(endpoint: endpoint, token: paired.token)
        do {
            try verify(paired.serverID == health.serverID && paired.apiVersion == 1 && paired.device.platform == .macos,
                       "Pairing identity does not match health.")
            let conversation = try await api.createConversation(title: "Swift 通信验证")
            let renamed = try await api.updateConversation(conversation.id, title: "Swift 中文通信验证")
            try verify(renamed.title == "Swift 中文通信验证", "Conversation rename did not persist.")
            let list = try await api.conversations(query: "中文通信", includeArchived: true)
            try verify(list.contains { $0.id == conversation.id }, "Conversation search missed the new conversation.")
            let clientMessageID = UUID()
            let text = "你好，请说明当前是否为演示模式。"
            var state = ClientState()
            state.upsertConversation(conversation)
            state.enqueue(content: text, conversationID: conversation.id, id: clientMessageID)
            let sent = try await api.sendMessage(conversationID: conversation.id, clientMessageID: clientMessageID, content: text)
            try state.accept(sent, clientMessageID: clientMessageID)
            let duplicate = try await api.sendMessage(conversationID: conversation.id, clientMessageID: clientMessageID, content: text)
            try verify(duplicate.status == "duplicate" && duplicate.runID == sent.runID && duplicate.message.id == sent.message.id,
                       "Retry created a different request or run.")
            var completed = false
            var failed = false
            let deadline = Date().addingTimeInterval(40)
            while Date() < deadline, !completed, !failed {
                let page = try await api.events(after: state.cursor, limit: 2)
                state = try state.applying(page)
                completed = state.messages(in: conversation.id).contains {
                    $0.role == .assistant && $0.runID == sent.runID && $0.status == .completed
                }
                failed = state.partialReplies.contains { $0.id == sent.runID && $0.error != nil } ||
                    state.messages(in: conversation.id).contains { $0.runID == sent.runID && $0.status == .failed }
                if !page.hasMore, !completed, !failed { try await Task.sleep(nanoseconds: 100_000_000) }
            }
            try verify(completed && !failed, "No successful final reply within 40 seconds.")
            let messages = try await api.messages(conversationID: conversation.id)
            let users = messages.filter { $0.role == .user && $0.clientMessageID == clientMessageID }
            let assistants = messages.filter { $0.role == .assistant && $0.runID == sent.runID && $0.status == .completed }
            try verify(users.count == 1 && users.first?.content == text && assistants.count == 1,
                       "History lost Unicode text or contains a duplicated run.")
            let archived = try await api.updateConversation(conversation.id, archived: true)
            try verify(archived.archived, "Archive state did not persist.")
            try await api.revokeOwnDevice(paired.device.id)
            var rejected = false
            do { _ = try await api.conversations() }
            catch let error as ClientError { rejected = error.requiresPairing }
            try verify(rejected, "A revoked token still accessed the service.")
        } catch {
            try? await api.revokeOwnDevice(paired.device.id)
            throw error
        }
    }
}
