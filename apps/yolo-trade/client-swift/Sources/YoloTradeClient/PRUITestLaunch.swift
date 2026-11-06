import Foundation
import SwiftUI
import YoloAgentsSDK

private typealias PRLaunchState<Value> = SwiftUI.State<Value>

struct PRUITestLaunchError: Error {}

/// Parsed before any repository is opened. No test setting can select production storage.
struct PRUITestLaunchConfiguration: Equatable, Sendable {
    static let environmentPrefix = "YOLO_TRADE_PR_UI_"
    let runID: String
    let endpoint: ServiceEndpoint
    let pairingCode: String
    let platform: DevicePlatform
    let cleanup: Bool

    var identity: AgentAppIdentity {
        AgentAppIdentity(
            agentID: YoloTradeProduct.agentID,
            appName: YoloTradeProduct.displayName + " PR UI",
            keychainService: YoloTradeProduct.keychainService + ".prtest.\(runID).\(platform.rawValue)",
            defaultServiceAddress: endpoint.url.absoluteString
        )
    }

    static func parse(environment: [String: String], bundleID: String?, testBuild: Bool,
                      debugBuild: Bool, platform: DevicePlatform) throws -> Self? {
        // Release ignores every launch override, including an accidental inherited test environment.
        guard debugBuild else { return nil }
        let requested = environment.keys.contains { $0.hasPrefix(environmentPrefix) }
        guard requested || testBuild else { return nil }
        guard testBuild,
              let runID = environment[environmentPrefix + "RUN_ID"],
              runID.range(of: "^[a-f0-9]{12}4[a-f0-9]{3}[89ab][a-f0-9]{15}$", options: .regularExpression) != nil,
              bundleID == YoloTradeProduct.prTestBundleIdentifierPrefix + runID,
              environment[environmentPrefix + "PLATFORM"] == platform.rawValue,
              let address = environment[environmentPrefix + "ENDPOINT"],
              let url = URLComponents(string: address),
              url.scheme == "http", url.host == "127.0.0.1",
              let port = url.port, (1024...65535).contains(port),
              let productionPort = URLComponents(string: YoloTradeProduct.defaultServiceAddress)?.port,
              port != productionPort,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/",
              let code = environment[environmentPrefix + "PAIRING_CODE"],
              (6...256).contains(code.count),
              !code.unicodeScalars.contains(where: { CharacterSet.whitespacesAndNewlines.contains($0) }),
              [nil, "0", "1"].contains(environment[environmentPrefix + "CLEANUP"]) else {
            throw PRUITestLaunchError()
        }
        return Self(runID: runID, endpoint: try ServiceEndpoint(address, allowInsecureLocalHTTP: true),
                    pairingCode: code, platform: platform,
                    cleanup: environment[environmentPrefix + "CLEANUP"] == "1")
    }

    /// Pairing and catalog checks prepare the ordinary product UI. Messages are
    /// exclusively composed and sent by the XCUITest through that UI.
    func prepare(repository: ClientRepository? = nil,
                 transport: any HTTPTransport = URLSessionTransport()) async throws {
        let repository = repository ?? ClientRepository(identity: identity)
        guard repository.identity == identity else { throw PRUITestLaunchError() }
        if cleanup {
            if let connection = try repository.connection() { try repository.forget(connection) }
            return
        }
        let publicAPI = APIClient(endpoint: endpoint, transport: transport)
        let health = try await publicAPI.health()
        try identity.validate(health)
        guard health.status == "ok", health.agentMode == "chatgpt" else { throw PRUITestLaunchError() }
        let connection: PairedConnection
        if let stored = try repository.connection() {
            guard stored.endpoint == endpoint, stored.pairing.serverID == health.serverID,
                  stored.pairing.device.platform == platform,
                  stored.pairing.device.revokedAt == nil, stored.pairing.apiVersion == 1,
                  !stored.pairing.token.isEmpty else { throw PRUITestLaunchError() }
            connection = stored
        } else {
            let pairing = try await publicAPI.pair(code: pairingCode, name: "PR UI \(platform.rawValue) \(runID)",
                                                   platform: platform)
            guard pairing.serverID == health.serverID, pairing.apiVersion == 1,
                  pairing.device.platform == platform, pairing.device.revokedAt == nil,
                  !pairing.token.isEmpty else { throw PRUITestLaunchError() }
            connection = PairedConnection(endpoint: endpoint, pairing: pairing, agentID: YoloTradeProduct.agentID)
            // Save immediately so failure cleanup can remove only this run's records.
            try repository.saveConnection(connection)
        }
        let catalog = try await APIClient(endpoint: endpoint, token: connection.pairing.token,
                                          transport: transport).models()
        guard !catalog.models.isEmpty, catalog.auth?.needsLogin != true, catalog.error == nil else { throw PRUITestLaunchError() }
    }
}

/// Both platform entry points assemble this same product-owned startup boundary.
public struct YoloTradeAppEntryView: View {
    public init() {}
    public var body: some View {
        #if DEBUG
        PRUITestEntryBoundary()
        #else
        YoloTradeRootView()
        #endif
    }
}

#if DEBUG
private struct PRUITestEntryBoundary: View {
    @PRLaunchState private var ready = false
    @PRLaunchState private var failed = false
    private let configuration: PRUITestLaunchConfiguration?
    private let invalid: Bool

    init() {
        #if os(macOS)
        let platform: DevicePlatform = .macos
        #else
        let platform: DevicePlatform = .ios
        #endif
        do {
            configuration = try PRUITestLaunchConfiguration.parse(
                environment: ProcessInfo.processInfo.environment, bundleID: Bundle.main.bundleIdentifier,
                testBuild: Bundle.main.object(forInfoDictionaryKey: "YoloTradePRTestBuild") as? String == "YES",
                debugBuild: true, platform: platform)
            invalid = false
        } catch {
            configuration = nil
            invalid = true
        }
    }

    var body: some View {
        Group {
            if invalid || failed {
                Text("PR UI 启动检查未通过").accessibilityIdentifier("prtest.startup.failed")
            } else if let configuration {
                if ready && configuration.cleanup {
                    Text("PR UI 测试记录已清理").accessibilityIdentifier("prtest.cleanup.complete")
                } else if ready {
                    YoloTradeRootView(prTestIdentity: configuration.identity)
                } else {
                    ProgressView("正在连接临时测试服务…")
                }
            } else {
                YoloTradeRootView()
            }
        }
        .task {
            guard let configuration, !invalid, !ready else { return }
            do { try await configuration.prepare(); ready = true }
            catch { failed = true }
        }
    }
}
#endif
