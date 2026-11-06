import Foundation
import Testing
import YoloAgentsSDK
@testable import YoloTradeClient

private let prRun = "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"
private let prServer = "40000000-0000-4000-8000-000000000004"
private func prEnvironment(run: String = prRun, platform: DevicePlatform = .macos) -> [String: String] {
    ["YOLO_TRADE_PR_UI_RUN_ID": run, "YOLO_TRADE_PR_UI_ENDPOINT": "http://127.0.0.1:49152",
     "YOLO_TRADE_PR_UI_PAIRING_CODE": "fixture-once-only", "YOLO_TRADE_PR_UI_PLATFORM": platform.rawValue]
}
private func prConfiguration(_ environment: [String: String] = prEnvironment(),
                             platform: DevicePlatform = .macos) throws -> PRUITestLaunchConfiguration {
    try #require(PRUITestLaunchConfiguration.parse(
        environment: environment, bundleID: "com.yoloagents.yolotrade.prtest." + (environment["YOLO_TRADE_PR_UI_RUN_ID"] ?? ""),
        testBuild: true, debugBuild: true, platform: platform))
}

private final class PRMemoryStore: SecureStore, @unchecked Sendable {
    private let lock = NSLock()
    private var records: [String: Data] = [:]
    func read(_ key: String) -> Data? { lock.withLock { records[key] } }
    func write(_ data: Data, for key: String) { lock.withLock { records[key] = data } }
    func remove(_ key: String) { _ = lock.withLock { records.removeValue(forKey: key) } }
}
private actor PRBootstrapHTTP: HTTPTransport {
    let agentID: String
    let modelsAvailable: Bool
    private(set) var requests: [URLRequest] = []
    init(agentID: String = "yolo-trade", modelsAvailable: Bool = true) {
        self.agentID = agentID; self.modelsAvailable = modelsAvailable
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        let body: [String: Any]
        switch request.url?.path {
        case "/v1/health":
            body = ["status": "ok", "api_version": 1, "server_id": prServer,
                    "agent_mode": "chatgpt", "agent_id": agentID]
        case "/v1/devices/pair":
            body = ["api_version": 1, "server_id": prServer, "token": "fixture-ephemeral-token",
                    "device": ["id": "10000000-0000-4000-8000-000000000001", "name": "PR UI fixture",
                               "platform": "macos", "permissions": ["chat:read", "chat:write"],
                               "created_at": "2026-11-06T00:00:00Z"]]
        case "/v1/models":
            body = ["models": modelsAvailable ? [["id": "fixture-model"]] : [], "auth": ["authenticated": modelsAvailable]]
        default: throw PRUITestLaunchError()
        }
        return HTTPResult(data: try JSONSerialization.data(withJSONObject: body), status: 200)
    }
}

@Test func prUILaunchRequiresDebugTestBundleAndCompleteNamespace() throws {
    let configuration = try prConfiguration()
    #expect(configuration.identity.agentID == "yolo-trade")
    #expect(configuration.identity.keychainService == "com.yoloagents.yolo-trade.apple.v1.prtest.\(prRun).macos")
    #expect(configuration.identity.defaultServiceAddress == "http://127.0.0.1:49152")
    #expect(!configuration.cleanup)
    #expect(throws: PRUITestLaunchError.self) {
        try PRUITestLaunchConfiguration.parse(environment: prEnvironment(), bundleID: "com.yoloagents.yolotrade",
                                               testBuild: true, debugBuild: true, platform: .macos)
    }
    #expect(throws: PRUITestLaunchError.self) {
        try PRUITestLaunchConfiguration.parse(environment: prEnvironment(), bundleID: "com.yoloagents.yolotrade.prtest.\(prRun)",
                                               testBuild: false, debugBuild: true, platform: .macos)
    }
    for key in prEnvironment().keys {
        var incomplete = prEnvironment(); incomplete.removeValue(forKey: key)
        #expect(throws: PRUITestLaunchError.self) { try prConfiguration(incomplete) }
    }
}

@Test func prUIRejectsProductionRemoteEndpointsAndInvalidLaunchValues() throws {
    for endpoint in ["http://127.0.0.1:8765", "https://127.0.0.1:49152", "http://example.test:49152",
                     "http://127.0.0.1:49152/v1", "http://user@127.0.0.1:49152", "http://127.0.0.1:49152?token=x"] {
        var environment = prEnvironment(); environment["YOLO_TRADE_PR_UI_ENDPOINT"] = endpoint
        #expect(throws: PRUITestLaunchError.self) { try prConfiguration(environment) }
    }
    for (key, value) in [("RUN_ID", "../production"), ("PAIRING_CODE", "x\ny"), ("PLATFORM", "ios"), ("CLEANUP", "yes")] {
        var environment = prEnvironment(); environment["YOLO_TRADE_PR_UI_" + key] = value
        #expect(throws: PRUITestLaunchError.self) { try prConfiguration(environment) }
    }
}

@Test func releaseAndOrdinaryDebugLaunchKeepTheProductionPath() throws {
    let release = try PRUITestLaunchConfiguration.parse(
        environment: prEnvironment(), bundleID: "com.yoloagents.yolotrade", testBuild: true, debugBuild: false, platform: .macos)
    #expect(release == nil)
    let ordinary = try PRUITestLaunchConfiguration.parse(
        environment: [:], bundleID: "com.yoloagents.yolotrade", testBuild: false, debugBuild: true, platform: .macos)
    #expect(ordinary == nil)
    #expect(throws: PRUITestLaunchError.self) {
        try PRUITestLaunchConfiguration.parse(environment: [:], bundleID: "com.yoloagents.yolotrade.prtest.\(prRun)",
                                               testBuild: true, debugBuild: true, platform: .macos)
    }
}

@Test func prUINamespacesAreDisjointAcrossRunsPlatformsAndProduction() throws {
    let mac = try prConfiguration()
    let ios = try prConfiguration(prEnvironment(platform: .ios), platform: .ios)
    let other = try prConfiguration(prEnvironment(run: "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb"))
    #expect(Set([mac.identity.keychainService, ios.identity.keychainService, other.identity.keychainService,
                 YoloTradeProduct.identity.keychainService]).count == 4)
    // Cache and pairing share the repository's service boundary, so both are isolated.
    #expect(mac.identity.agentID == ios.identity.agentID)
}

@Test func prUIBootstrapReusesItsPairingAndNeverSendsAChatThroughTheSDK() async throws {
    let configuration = try prConfiguration()
    let memory = PRMemoryStore()
    let repository = ClientRepository(identity: configuration.identity, store: memory)
    let http = PRBootstrapHTTP()
    try await configuration.prepare(repository: repository, transport: http)
    let saved = try #require(repository.connection())
    try repository.saveState(ClientState(), for: saved)
    try await configuration.prepare(repository: repository, transport: http)
    #expect(await http.requests.map { $0.url?.path } == ["/v1/health", "/v1/devices/pair", "/v1/models", "/v1/health", "/v1/models"])
    #expect(try repository.connection() == saved)
    var environment = prEnvironment(); environment["YOLO_TRADE_PR_UI_CLEANUP"] = "1"
    let cleanup = try prConfiguration(environment)
    try await cleanup.prepare(repository: repository, transport: http)
    #expect(try repository.connection() == nil)
    #expect(try repository.state(for: saved).conversations.isEmpty)
    #expect(await http.requests.count == 5)
    // Even a test-only bootstrap cannot open a supplied production repository.
    let production = ClientRepository(identity: YoloTradeProduct.identity, store: PRMemoryStore())
    await #expect(throws: PRUITestLaunchError.self) {
        try await cleanup.prepare(repository: production, transport: http)
    }
}

@Test func prUIWrongHostAndMissingModelsFailBeforeTheChatUIStarts() async throws {
    let configuration = try prConfiguration()
    let wrongHost = PRBootstrapHTTP(agentID: "other-product")
    await #expect(throws: (any Error).self) {
        try await configuration.prepare(repository: ClientRepository(identity: configuration.identity, store: PRMemoryStore()),
                                        transport: wrongHost)
    }
    #expect(await wrongHost.requests.count == 1)
    let noModels = PRBootstrapHTTP(modelsAvailable: false)
    await #expect(throws: PRUITestLaunchError.self) {
        try await configuration.prepare(repository: ClientRepository(identity: configuration.identity, store: PRMemoryStore()),
                                        transport: noModels)
    }
}
