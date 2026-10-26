import Foundation
import Testing
@testable import YoloAgentsSDK

private let liveEnvironmentIsConfigured =
    ProcessInfo.processInfo.environment["YOLO_AGENTS_TEST_URL"]?.isEmpty == false &&
    ProcessInfo.processInfo.environment["YOLO_AGENTS_PAIRING_CODE"]?.isEmpty == false

/// Opt-in, destructive only to the device/conversation created by this test.
/// The service must be a disposable demo instance. No token/code is logged or cached.
@Test(.enabled(if: liveEnvironmentIsConfigured,
               "Set YOLO_AGENTS_TEST_URL and YOLO_AGENTS_PAIRING_CODE for a disposable demo service."))
func liveServiceRoundTrip() async throws {
    let environment = ProcessInfo.processInfo.environment
    guard let address = environment["YOLO_AGENTS_TEST_URL"],
          let code = environment["YOLO_AGENTS_PAIRING_CODE"] else { return }
    guard let platform = DevicePlatform(rawValue: environment["YOLO_AGENTS_TEST_PLATFORM"] ?? "macos") else {
        Issue.record("YOLO_AGENTS_TEST_PLATFORM must be ios or macos.")
        return
    }
    let endpoint = try ServiceEndpoint(address, allowInsecureLocalHTTP: true)
    let publicAPI = APIClient(endpoint: endpoint)
    let health = try await publicAPI.health()
    let mode = health.agentMode
    guard mode == "demo" else {
        Issue.record("Live SDK test requires agent_mode=demo to avoid unintended model calls.")
        return
    }
    let paired = try await publicAPI.pair(code: code, name: "Swift SDK temporary integration test", platform: platform)
    let matchesServer = paired.serverID == health.serverID
    let matchesPlatform = paired.device.platform == platform
    #expect(matchesServer && matchesPlatform)
    let api = APIClient(endpoint: endpoint, token: paired.token)
    let conversation = try await api.createConversation(title: "Swift 中文联调")
    let clientMessageID = UUID()
    let text = "你好，请说明当前是否为演示模式。"
    let sent = try await api.sendMessage(conversationID: conversation.id, clientMessageID: clientMessageID, content: text)
    let duplicate = try await api.sendMessage(conversationID: conversation.id, clientMessageID: clientMessageID, content: text)
    let didDeduplicate = duplicate.status == "duplicate" && duplicate.runID == sent.runID && duplicate.message.id == sent.message.id
    #expect(didDeduplicate)
    var state = ClientState()
    var sawCompletion = false
    var sawFailure = false
    let deadline = Date().addingTimeInterval(40)
    while Date() < deadline, !sawCompletion, !sawFailure {
        let page = try await api.events(after: state.cursor, limit: 2)
        state = try state.applying(page)
        sawCompletion = state.messages(in: conversation.id).contains {
            $0.role == .assistant && $0.runID == sent.runID && $0.status == .completed
        }
        sawFailure = state.partialReplies.contains { $0.id == sent.runID && $0.error != nil } ||
            state.messages(in: conversation.id).contains { $0.runID == sent.runID && $0.status == .failed }
        if !page.hasMore, !sawCompletion, !sawFailure { try await Task.sleep(nanoseconds: 100_000_000) }
    }
    #expect(sawCompletion && !sawFailure)
    let messages = try await api.messages(conversationID: conversation.id)
    let userCount = messages.filter { $0.role == .user && $0.clientMessageID == clientMessageID }.count
    let assistantCount = messages.filter { $0.role == .assistant && $0.runID == sent.runID && $0.status == .completed }.count
    #expect(userCount == 1 && assistantCount == 1)

    // Revoke only the temporary device minted above, then verify its SDK credential fails.
    try await api.revokeOwnDevice(paired.device.id)
    var rejected = false
    do { _ = try await api.conversations() }
    catch let error as ClientError { rejected = error.requiresPairing }
    #expect(rejected)
}
