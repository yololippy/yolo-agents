import Foundation
import Testing
@testable import YoloAgentsSDK

private let tradingIdentity = AgentAppIdentity(agentID: "trading-agent", appName: "Trading Agent",
    keychainService: "com.tradingagent.apple.v1", defaultServiceAddress: "http://127.0.0.1:8765", allowsLegacyServer: true)
private let bIdentity = AgentAppIdentity(agentID: "b-agent", appName: "B Agent",
    keychainService: "com.yoloagents.b-agent.apple.v1", defaultServiceAddress: "http://127.0.0.1:8766")
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
    try tradingIdentity.validate(identityHealth("trading-agent"))
    try bIdentity.validate(identityHealth("b-agent"))
    try tradingIdentity.validate(identityHealth(nil))
    #expect(throws: ClientError.agentIdentityMismatch) { try bIdentity.validate(identityHealth("trading-agent")) }
    #expect(throws: ClientError.agentIdentityMismatch) { try tradingIdentity.validate(identityHealth("b-agent")) }
    #expect(throws: ClientError.agentIdentityUnavailable) { try bIdentity.validate(identityHealth(nil)) }
    #expect(throws: ClientError.incompatibleServer) { try bIdentity.validate(identityHealth("b-agent"), serverID: UUID()) }
    #expect(throws: ClientError.agentIdentityMismatch) { try bIdentity.validate(identityConnection("trading-agent")) }
}

@Test func scopedStoragePreservesTradingMigrationAndNeverImportsItIntoB() throws {
    let memory = IdentityMemory()
    let original = ClientRepository(store: memory)
    let legacyConnection = try identityConnection()
    try original.saveConnection(legacyConnection)
    var tradingState = ClientState()
    let date = Date(timeIntervalSince1970: 1_800_000_000)
    tradingState.upsertConversation(Conversation(id: UUID(), title: "Trading only", createdAt: date, updatedAt: date))
    try original.saveState(tradingState, for: legacyConnection)
    let trading = ClientRepository(identity: tradingIdentity, store: memory)
    let b = ClientRepository(identity: bIdentity, store: memory)
    #expect(try trading.connection() == legacyConnection)
    #expect(try trading.state(for: legacyConnection).conversations.first?.title == "Trading only")
    #expect(try b.connection() == nil)
    let bConnection = try identityConnection("b-agent")
    try b.saveConnection(bConnection)
    #expect(try b.state(for: bConnection).conversations.isEmpty)
    var bState = ClientState()
    bState.upsertConversation(Conversation(id: UUID(), title: "B only", createdAt: date, updatedAt: date))
    try b.saveState(bState, for: bConnection)
    #expect(try trading.state(for: legacyConnection).conversations.first?.title == "Trading only")
    #expect(try b.state(for: bConnection).conversations.first?.title == "B only")
    #expect(throws: ClientError.agentIdentityUnavailable) { try b.saveConnection(legacyConnection) }
    try b.forget(bConnection)
    #expect(try b.connection() == nil && trading.connection() == legacyConnection)
    #expect(tradingIdentity.keychainService != bIdentity.keychainService)
}

@Test func providerAccountDetailsErrorsAndCancellationDecode() throws {
    let data = Data("""
    {"account":{"status":"login_required","method":"chatgpt","can_manage":true,"agent_id":"b-agent","app_name":"B Agent",
    "error":{"code":"remote_revocation_unconfirmed"}}}
    """.utf8)
    let account = try WireJSON.decoder().decode(ChatGPTAccountStatus.self, from: data)
    #expect(account.agentID == "b-agent" && account.appName == "B Agent" && account.canManage == true)
    #expect(account.method == "chatgpt" && account.error?.message.contains("远程授权撤销尚未确认") == true)
    #expect(try WireJSON.decoder().decode(AccountLoginCancellation.self, from: data).isCancelled)
}

@Test func nonTradingHealthOmitsExecutionCapabilityWithoutGrantingIt() throws {
    var object: [String: Any] = ["status": "ok", "api_version": 1,
        "server_id": "40000000-0000-4000-8000-000000000004", "agent_mode": "chatgpt",
        "agent_id": "b-agent", "app_name": "B Agent", "model": NSNull()]
    func decode() throws -> Health {
        try WireJSON.decoder().decode(Health.self, from: JSONSerialization.data(withJSONObject: object))
    }
    let health = try decode()
    #expect(!health.tradeExecutionEnabled && health.agentID == "b-agent" && health.appName == "B Agent")
    try bIdentity.validate(health)
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
