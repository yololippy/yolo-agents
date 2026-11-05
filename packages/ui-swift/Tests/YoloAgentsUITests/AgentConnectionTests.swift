import Foundation
import Testing
import YoloAgentsSDK
@testable import YoloAgentsUI

private let connectionIdentity = AgentAppIdentity(agentID: "yolo-beni", appName: "YoloBeni", keychainService: "fixture.yolo-beni", defaultServiceAddress: "http://127.0.0.1:8766")
private actor PairingIdentityHTTP: HTTPTransport {
    let healthAgent: String?
    private(set) var requests: [URLRequest] = []
    init(_ healthAgent: String?) { self.healthAgent = healthAgent }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        var value: [String: Any] = ["status": "ok", "api_version": 1, "server_id": "40000000-0000-4000-8000-000000000004", "agent_mode": "chatgpt", "trade_execution_enabled": false]
        value["agent_id"] = healthAgent
        return HTTPResult(data: try JSONSerialization.data(withJSONObject: value), status: 200)
    }
}
private final class ConnectionMemory: SecureStore, @unchecked Sendable {
    private let lock = NSLock()
    private var data: [String: Data] = [:]
    func read(_ key: String) -> Data? { lock.withLock { data[key] } }
    func write(_ value: Data, for key: String) { lock.withLock { data[key] = value } }
    func remove(_ key: String) { _ = lock.withLock { data.removeValue(forKey: key) } }
}

private actor YoloBeniPairHTTP: HTTPTransport {
    private(set) var requests: [URLRequest] = []
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        let body: String
        if request.url?.path == "/v1/health" {
            body = """
            {"status":"ok","api_version":1,"server_id":"40000000-0000-4000-8000-000000000004",
             "agent_mode":"chatgpt","model":null,"agent_id":"yolo-beni","app_name":"YoloBeni"}
            """
        } else {
            body = """
            {"device":{"id":"10000000-0000-4000-8000-000000000001","name":"Fixture","platform":"macos",
             "permissions":["chat:read","chat:write","devices:manage"],"created_at":"2026-10-28T00:00:00Z"},
             "token":"fixture-pair-token","server_id":"40000000-0000-4000-8000-000000000004","api_version":1}
            """
        }
        return HTTPResult(data: Data(body.utf8), status: 200)
    }
}

@Test @MainActor func yoloBeniHealthAllowsPairingAndPersistsCorrectAppIdentity() async throws {
    let http = YoloBeniPairHTTP()
    let repository = ClientRepository(identity: connectionIdentity, store: ConnectionMemory())
    let model = try ChatModel(identity: connectionIdentity, repository: repository, transport: http)
    model.setActive(false)
    let paired = await model.pair(address: "https://example.test", code: "fixture-pairing", deviceName: "Fixture", allowHTTP: false)
    #expect(paired && model.errorMessage == nil && model.appName == "YoloBeni")
    #expect(model.health?.tradeExecutionEnabled == false && model.connection?.agentID == "yolo-beni")
    #expect(try repository.connection()?.agentID == "yolo-beni")
    #expect(await http.requests.map { $0.url?.path } == ["/v1/health", "/v1/devices/pair"])
}

@Test @MainActor func pairingChecksAgentBeforeSendingPairingCodeOrToken() async throws {
    for healthAgent in ["yolo-trade", nil] {
        let http = PairingIdentityHTTP(healthAgent)
        let model = try ChatModel(identity: connectionIdentity,
            repository: ClientRepository(identity: connectionIdentity, store: ConnectionMemory()), transport: http)
        model.setActive(false)
        let paired = await model.pair(address: "https://example.test", code: "fixture-pairing", deviceName: "Fixture", allowHTTP: false)
        #expect(!paired && model.connection == nil && model.errorMessage != nil)
        #expect(await http.requests.count == 1)
        #expect(await http.requests.first?.url?.path == "/v1/health")
        #expect(await http.requests.first?.httpBody == nil)
        #expect(await http.requests.first?.value(forHTTPHeaderField: "Authorization") == nil)
    }
}

@Test @MainActor func restoredConnectionValidatesHealthBeforeAuthenticatedSync() async throws {
    let http = PairingIdentityHTTP("yolo-trade")
    let pairing = try WireJSON.decoder().decode(PairingResponse.self, from: Data("""
    {"device":{"id":"10000000-0000-4000-8000-000000000001","name":"Fixture","platform":"macos","permissions":["chat:read"],
    "created_at":"2026-10-28T00:00:00Z"},"token":"fixture-token","server_id":"40000000-0000-4000-8000-000000000004","api_version":1}
    """.utf8))
    let repository = ClientRepository(identity: connectionIdentity, store: ConnectionMemory())
    try repository.saveConnection(PairedConnection(endpoint: ServiceEndpoint("https://example.test"), pairing: pairing, agentID: "yolo-beni"))
    let model = try ChatModel(identity: connectionIdentity, repository: repository, transport: http)
    await model.start()
    defer { model.setActive(false) }
    let deadline = ContinuousClock.now.advanced(by: .seconds(2))
    while model.connectionState != .authorizationRequired && ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(5)) }
    #expect(model.connectionState == .authorizationRequired && model.errorMessage != nil)
    #expect(await http.requests.count == 1)
    #expect(await http.requests.first?.value(forHTTPHeaderField: "Authorization") == nil)
}
