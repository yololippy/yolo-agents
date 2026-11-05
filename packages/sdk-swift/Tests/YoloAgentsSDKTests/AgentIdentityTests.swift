import Foundation
import Testing
@testable import YoloAgentsSDK

private let yoloTradeIdentity = AgentAppIdentity(agentID: "yolo-trade", appName: "YoloTrade",
    keychainService: "com.yoloagents.yolo-trade.apple.v1", defaultServiceAddress: "http://127.0.0.1:8765", allowsLegacyServer: true)
private let yoloBeniIdentity = AgentAppIdentity(agentID: "yolo-beni", appName: "YoloBeni",
    keychainService: "com.yoloagents.yolo-beni.apple.v1", defaultServiceAddress: "http://127.0.0.1:8766")
private final class IdentityMemory: SecureStore, @unchecked Sendable {
    private let lock = NSLock()
    private var data: [String: Data] = [:]
    func read(_ key: String) -> Data? { lock.withLock { data[key] } }
    func write(_ value: Data, for key: String) { lock.withLock { data[key] = value } }
    func remove(_ key: String) { _ = lock.withLock { data.removeValue(forKey: key) } }
}
private func identityConnection(_ agentID: String? = nil) throws -> PairedConnection {
    let pairing = try WireJSON.decoder().decode(PairingResponse.self, from: Data("""
    {"device":{"id":"10000000-0000-4000-8000-000000000001","name":"Fixture","platform":"macos","permissions":["devices:manage"],
    "created_at":"2026-10-28T00:00:00Z"},"token":"fixture-token","server_id":"40000000-0000-4000-8000-000000000004","api_version":1}
    """.utf8))
    return PairedConnection(endpoint: try ServiceEndpoint("https://example.test"), pairing: pairing, agentID: agentID)
}
private func identityHealth(_ id: String?) throws -> Health {
    var object: [String: Any] = ["status": "ok", "api_version": 1, "server_id": "40000000-0000-4000-8000-000000000004",
                               "agent_mode": "chatgpt", "trade_execution_enabled": false]
    object["agent_id"] = id
    return try WireJSON.decoder().decode(Health.self, from: JSONSerialization.data(withJSONObject: object))
}

@Test func agentIdentityRejectsCrossConnectionsAndLimitsLegacyCompatibility() throws {
    try yoloTradeIdentity.validate(identityHealth("yolo-trade"))
    try yoloBeniIdentity.validate(identityHealth("yolo-beni"))
    try yoloTradeIdentity.validate(identityHealth(nil))
    #expect(throws: ClientError.agentIdentityMismatch) { try yoloBeniIdentity.validate(identityHealth("yolo-trade")) }
    #expect(throws: ClientError.agentIdentityMismatch) { try yoloTradeIdentity.validate(identityHealth("yolo-beni")) }
    #expect(throws: ClientError.agentIdentityUnavailable) { try yoloBeniIdentity.validate(identityHealth(nil)) }
    #expect(throws: ClientError.incompatibleServer) { try yoloBeniIdentity.validate(identityHealth("yolo-beni"), serverID: UUID()) }
    #expect(throws: ClientError.agentIdentityMismatch) { try yoloBeniIdentity.validate(identityConnection("yolo-trade")) }
}

@Test func scopedStoragePreservesYoloTradeMigrationAndNeverImportsItIntoYoloBeni() throws {
    let memory = IdentityMemory()
    let legacyConnection = try identityConnection()
    var yoloTradeState = ClientState()
    let date = Date(timeIntervalSince1970: 1_800_000_000)
    yoloTradeState.upsertConversation(Conversation(id: UUID(), title: "YoloTrade only", createdAt: date, updatedAt: date))
    try memory.write(WireJSON.encoder().encode(legacyConnection), for: "connection")
    try memory.write(WireJSON.encoder().encode(yoloTradeState.cacheSnapshot()), for: legacyConnection.cacheKey)
    let yoloTrade = ClientRepository(identity: yoloTradeIdentity, store: memory)
    let yoloBeni = ClientRepository(identity: yoloBeniIdentity, store: memory)
    #expect(yoloTrade.identity == yoloTradeIdentity)
    #expect(yoloBeni.identity == yoloBeniIdentity)
    #expect(try yoloTrade.connection() == legacyConnection)
    #expect(try yoloTrade.state(for: legacyConnection).conversations.first?.title == "YoloTrade only")
    #expect(try yoloBeni.connection() == nil)
    let yoloBeniConnection = try identityConnection("yolo-beni")
    try yoloBeni.saveConnection(yoloBeniConnection)
    #expect(try yoloBeni.state(for: yoloBeniConnection).conversations.isEmpty)
    var yoloBeniState = ClientState()
    yoloBeniState.upsertConversation(Conversation(id: UUID(), title: "YoloBeni only", createdAt: date, updatedAt: date))
    try yoloBeni.saveState(yoloBeniState, for: yoloBeniConnection)
    #expect(try yoloTrade.state(for: legacyConnection).conversations.first?.title == "YoloTrade only")
    #expect(try yoloBeni.state(for: yoloBeniConnection).conversations.first?.title == "YoloBeni only")
    #expect(throws: ClientError.agentIdentityUnavailable) { try yoloBeni.saveConnection(legacyConnection) }
    try yoloBeni.forget(yoloBeniConnection)
    #expect(try yoloBeni.connection() == nil && yoloTrade.connection() == legacyConnection)
    #expect(yoloTradeIdentity.keychainService != yoloBeniIdentity.keychainService)
}

@Test func providerAccountDetailsErrorsAndCancellationDecode() throws {
    let data = Data("""
    {"account":{"status":"login_required","method":"chatgpt","can_manage":true,"agent_id":"yolo-beni","app_name":"YoloBeni",
    "error":{"code":"remote_revocation_unconfirmed"}}}
    """.utf8)
    let account = try WireJSON.decoder().decode(ChatGPTAccountStatus.self, from: data)
    #expect(account.agentID == "yolo-beni" && account.appName == "YoloBeni" && account.canManage == true)
    #expect(account.method == "chatgpt" && account.error?.message.contains("远程授权撤销尚未确认") == true)
    #expect(try WireJSON.decoder().decode(AccountLoginCancellation.self, from: data).isCancelled)
}

@Test func yoloBeniHealthOmitsExecutionCapabilityWithoutGrantingIt() throws {
    var object: [String: Any] = ["status": "ok", "api_version": 1,
        "server_id": "40000000-0000-4000-8000-000000000004", "agent_mode": "chatgpt",
        "agent_id": "yolo-beni", "app_name": "YoloBeni", "model": NSNull()]
    func decode() throws -> Health {
        try WireJSON.decoder().decode(Health.self, from: JSONSerialization.data(withJSONObject: object))
    }
    let health = try decode()
    #expect(!health.tradeExecutionEnabled && health.agentID == "yolo-beni" && health.appName == "YoloBeni")
    try yoloBeniIdentity.validate(health)
    object["trade_execution_enabled"] = true
    #expect(try decode().tradeExecutionEnabled)
    object["trade_execution_enabled"] = false
    #expect(try !decode().tradeExecutionEnabled)
    object["trade_execution_enabled"] = "true"
    #expect(throws: (any Error).self) { try decode() }
    object.removeValue(forKey: "trade_execution_enabled")
    object.removeValue(forKey: "server_id")
    #expect(throws: (any Error).self) { try decode() }
}
