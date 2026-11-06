import Foundation
import YoloAgentsSDK

public struct YoloTradeFullTestProbeResult: Codable, Equatable, Sendable {
    public let status: String
    public let agentID: String
    public let providerAuthenticated: Bool
    public let modelCatalogAvailable: Bool
    public let macClientOnline: Bool
    public let modelRoundtripPassed: Bool
    public let businessToolsEnabled: Bool
    public let conversationSaved: Bool
    public let model: String

    enum CodingKeys: String, CodingKey {
        case status, model
        case agentID = "agent_id"
        case providerAuthenticated = "provider_authenticated"
        case modelCatalogAvailable = "model_catalog_available"
        case macClientOnline = "mac_client_online"
        case modelRoundtripPassed = "model_roundtrip_passed"
        case businessToolsEnabled = "business_tools_enabled"
        case conversationSaved = "conversation_saved"
    }
}

public struct YoloTradeSimulatorProbeSession: Codable, Equatable, Sendable {
    public let endpoint: String
    public let pairingCode: String
    public let grant: String
    public let expiresIn: Int
    public let allowInsecureLocalHTTP: Bool

    public init(endpoint: String, pairingCode: String, grant: String, expiresIn: Int,
                allowInsecureLocalHTTP: Bool = false) {
        self.endpoint = endpoint
        self.pairingCode = pairingCode
        self.grant = grant
        self.expiresIn = expiresIn
        self.allowInsecureLocalHTTP = allowInsecureLocalHTTP
    }

    enum CodingKeys: String, CodingKey {
        case endpoint, grant
        case pairingCode = "pairing_code"
        case expiresIn = "expires_in"
        case allowInsecureLocalHTTP = "allow_insecure_local_http"
    }
}

public struct YoloTradeSimulatorProbeResult: Codable, Equatable, Sendable {
    public let status: String
    public let agentID: String
    public let modelCatalogAvailable: Bool
    public let iosClientOnline: Bool
    public let modelRoundtripPassed: Bool
    public let businessToolsEnabled: Bool
    public let conversationSaved: Bool
    public let model: String

    enum CodingKeys: String, CodingKey {
        case status, model
        case agentID = "agent_id"
        case modelCatalogAvailable = "model_catalog_available"
        case iosClientOnline = "ios_client_online"
        case modelRoundtripPassed = "model_roundtrip_passed"
        case businessToolsEnabled = "business_tools_enabled"
        case conversationSaved = "conversation_saved"
    }
}

private struct SimulatorSessionEnvelope: Decodable {
    struct Session: Decodable {
        let pairingCode: String
        let grant: String
        let expiresIn: Int
        enum CodingKeys: String, CodingKey {
            case grant
            case pairingCode = "pairing_code"
            case expiresIn = "expires_in"
        }
    }
    let session: Session
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
    public static func issueStoredSimulatorSession(
        endpointOverride: ServiceEndpoint? = nil,
        transport: any HTTPTransport = URLSessionTransport()
    ) async throws -> YoloTradeSimulatorProbeSession {
        let repository = ClientRepository(identity: YoloTradeProduct.identity)
        guard let connection = try repository.connection() else { throw FullTestAcceptanceFailure() }
        let target = endpointOverride.map {
            PairedConnection(endpoint: $0, pairing: connection.pairing, agentID: connection.agentID)
        } ?? connection
        return try await issueSimulatorSession(connection: target, transport: transport)
    }

    public static func issueSimulatorSession(
        connection: PairedConnection,
        transport: any HTTPTransport = URLSessionTransport()
    ) async throws -> YoloTradeSimulatorProbeSession {
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
              health.agentMode == "chatgpt",
              health.serverID == connection.pairing.serverID else {
            throw FullTestAcceptanceFailure()
        }
        let response: SimulatorSessionEnvelope = try await client.request(
            "POST", path: "admin/full-test/simulator-session", body: Data("{}".utf8)
        )
        guard response.session.expiresIn > 0,
              !response.session.pairingCode.isEmpty,
              !response.session.grant.isEmpty else {
            throw FullTestAcceptanceFailure()
        }
        return YoloTradeSimulatorProbeSession(
            endpoint: connection.endpoint.url.absoluteString,
            pairingCode: response.session.pairingCode,
            grant: response.session.grant,
            expiresIn: response.session.expiresIn,
            allowInsecureLocalHTTP: connection.endpoint.allowInsecureLocalHTTP
        )
    }

    public static func runSimulator(
        session: YoloTradeSimulatorProbeSession,
        transport: any HTTPTransport = URLSessionTransport()
    ) async throws -> YoloTradeSimulatorProbeResult {
        let endpoint = try ServiceEndpoint(
            session.endpoint, allowInsecureLocalHTTP: session.allowInsecureLocalHTTP
        )
        let publicClient = APIClient(endpoint: endpoint, transport: transport)
        let health = try await publicClient.health()
        guard health.status == "ok", health.agentID == YoloTradeProduct.agentID,
              health.agentMode == "chatgpt" else { throw FullTestAcceptanceFailure() }
        let pairing = try await publicClient.pair(
            code: session.pairingCode,
            name: "YoloTrade Full Test Simulator",
            platform: .ios
        )
        let connection = PairedConnection(
            endpoint: endpoint, pairing: pairing, agentID: YoloTradeProduct.agentID
        )
        do {
            try YoloTradeProduct.identity.validate(connection)
            guard pairing.serverID == health.serverID,
                  pairing.device.platform == .ios,
                  pairing.device.revokedAt == nil else {
                throw FullTestAcceptanceFailure()
            }
            let client = APIClient(endpoint: endpoint, token: pairing.token, transport: transport)
            let catalog = try await client.models()
            guard !catalog.models.isEmpty, catalog.auth?.needsLogin != true else {
                throw FullTestAcceptanceFailure()
            }
            struct Body: Encodable { let grant: String }
            let response: ModelProbeEnvelope = try await client.request(
                "POST", path: "full-test/model-probe",
                body: try WireJSON.encoder().encode(Body(grant: session.grant))
            )
            let probe = response.probe
            guard probe.status == "passed", probe.businessToolsEnabled == false,
                  probe.conversationSaved == false,
                  catalog.models.contains(where: { $0.id == probe.model }) else {
                throw FullTestAcceptanceFailure()
            }
            try await client.revokeOwnDevice(pairing.device.id)
            return YoloTradeSimulatorProbeResult(
                status: "passed",
                agentID: YoloTradeProduct.agentID,
                modelCatalogAvailable: true,
                iosClientOnline: true,
                modelRoundtripPassed: true,
                businessToolsEnabled: false,
                conversationSaved: false,
                model: probe.model
            )
        } catch {
            let client = APIClient(endpoint: endpoint, token: pairing.token, transport: transport)
            try? await client.revokeOwnDevice(pairing.device.id)
            throw error
        }
    }

    public static func runStored(
        endpointOverride: ServiceEndpoint? = nil,
        transport: any HTTPTransport = URLSessionTransport()
    ) async throws -> YoloTradeFullTestProbeResult {
        let repository = ClientRepository(identity: YoloTradeProduct.identity)
        guard let connection = try repository.connection() else { throw FullTestAcceptanceFailure() }
        let target = endpointOverride.map {
            PairedConnection(endpoint: $0, pairing: connection.pairing, agentID: connection.agentID)
        } ?? connection
        return try await run(connection: target, transport: transport)
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
        guard !(try await client.conversations(includeArchived: true)).isEmpty else {
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
            macClientOnline: true,
            modelRoundtripPassed: true,
            businessToolsEnabled: false,
            conversationSaved: false,
            model: probe.model
        )
    }
}
