import Foundation
import YoloAgentsSDK

public struct YoloTradeFullTestProbeResult: Codable, Equatable, Sendable {
    public let status: String
    public let agentID: String
    public let providerAuthenticated: Bool
    public let modelCatalogAvailable: Bool
    public let modelRoundtripPassed: Bool
    public let businessToolsEnabled: Bool
    public let conversationSaved: Bool
    public let model: String

    enum CodingKeys: String, CodingKey {
        case status, model
        case agentID = "agent_id"
        case providerAuthenticated = "provider_authenticated"
        case modelCatalogAvailable = "model_catalog_available"
        case modelRoundtripPassed = "model_roundtrip_passed"
        case businessToolsEnabled = "business_tools_enabled"
        case conversationSaved = "conversation_saved"
    }
}

private struct ModelProbeEnvelope: Decodable {
    struct Probe: Decodable {
        let status: String
        let model: String
        let businessToolsEnabled: Bool
        let conversationSaved: Bool
        enum CodingKeys: String, CodingKey {
            case status, model
            case businessToolsEnabled = "business_tools_enabled"
            case conversationSaved = "conversation_saved"
        }
    }
    let probe: Probe
}

private struct FullTestAcceptanceFailure: Error {}

public enum YoloTradeFullTestAcceptance {
    public static func runStored(
        transport: any HTTPTransport = URLSessionTransport()
    ) async throws -> YoloTradeFullTestProbeResult {
        let repository = ClientRepository(identity: YoloTradeProduct.identity)
        guard let connection = try repository.connection() else { throw FullTestAcceptanceFailure() }
        return try await run(connection: connection, transport: transport)
    }

    public static func run(
        connection: PairedConnection,
        transport: any HTTPTransport = URLSessionTransport()
    ) async throws -> YoloTradeFullTestProbeResult {
        try YoloTradeProduct.identity.validate(connection)
        guard connection.pairing.device.platform == .macos,
              connection.pairing.device.revokedAt == nil,
              connection.pairing.device.permissions.contains("devices:manage") else {
            throw FullTestAcceptanceFailure()
        }
        let client = APIClient(
            endpoint: connection.endpoint,
            token: connection.pairing.token,
            transport: transport
        )
        let health = try await client.health()
        guard health.status == "ok", health.agentID == YoloTradeProduct.agentID,
              health.agentMode == "chatgpt" else { throw FullTestAcceptanceFailure() }
        let account = try await client.account()
        guard account.isAuthenticated,
              account.agentID == nil || account.agentID == YoloTradeProduct.agentID else {
            throw FullTestAcceptanceFailure()
        }
        let catalog = try await client.models()
        guard !catalog.models.isEmpty, catalog.auth?.needsLogin != true else {
            throw FullTestAcceptanceFailure()
        }
        let response: ModelProbeEnvelope = try await client.request(
            "POST", path: "admin/full-test/model-probe", body: Data("{}".utf8)
        )
        let probe = response.probe
        guard probe.status == "passed", probe.businessToolsEnabled == false,
              probe.conversationSaved == false,
              catalog.models.contains(where: { $0.id == probe.model }) else {
            throw FullTestAcceptanceFailure()
        }
        return YoloTradeFullTestProbeResult(
            status: "passed",
            agentID: YoloTradeProduct.agentID,
            providerAuthenticated: true,
            modelCatalogAvailable: true,
            modelRoundtripPassed: true,
            businessToolsEnabled: false,
            conversationSaved: false,
            model: probe.model
        )
    }
}
