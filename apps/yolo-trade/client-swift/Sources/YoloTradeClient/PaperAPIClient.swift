import Foundation
import YoloAgentsSDK

public struct PaperHealth: Codable, Equatable, Sendable {
    public let status: String
    public let apiVersion: Int
    public let serverID: UUID
    public let agentMode: String
    public let model: String?
    public let tradeExecutionEnabled: Bool
    public let agentID: String?
    public let broker: PaperBrokerHealth?
    enum CodingKeys: String, CodingKey {
        case status, model, broker
        case apiVersion = "api_version", serverID = "server_id"
        case agentMode = "agent_mode", tradeExecutionEnabled = "trade_execution_enabled"
        case agentID = "agent_id"
    }
}


struct PaperAPIClient: Sendable {
    private let client: APIClient
    init(endpoint: ServiceEndpoint, token: String?, transport: any HTTPTransport = URLSessionTransport()) {
        client = APIClient(endpoint: endpoint, token: token, transport: transport)
    }
    static let tradeAuthorizationPath = "paper-trading/authorization"
    static let tradeDevicesPath = "paper-trading/devices"
    func positions() async throws -> TradePositionsResponse {
        let response: TradePositionsResponse = try await client.request("GET", path: "positions")
        guard response.count == response.positions.count else { throw PaperOrderValidationError.invalidState }
        return response
    }
    func search(_ query: String) async throws -> [TradeInstrument] {
        struct Response: Decodable { let instruments: [TradeInstrument] }
        let response: Response = try await client.request("GET", path: "instruments/search", query: [URLQueryItem(name: "q", value: query)])
        return Array(response.instruments.prefix(16))
    }
    func quote(_ conID: Int) async throws -> TradeQuote {
        struct Response: Decodable { let quote: TradeQuote }
        let response: Response = try await client.request("GET", path: "instruments/\(conID)/quote")
        guard response.quote.conID == conID else { throw PaperOrderValidationError.invalidState }
        return response.quote
    }
    func tradeAuthorization() async throws -> TradeDeviceAuthorization {
        struct Response: Decodable { let authorization: TradeDeviceAuthorization }
        let response: Response = try await client.request("GET", path: Self.tradeAuthorizationPath)
        return response.authorization
    }
    func tradeDevices() async throws -> [TradePairedPhone] {
        struct Response: Decodable { let devices: [TradePairedPhone] }
        let response: Response = try await client.request("GET", path: Self.tradeDevicesPath)
        return response.devices
    }
    func setTradeAuthorization(_ deviceID: UUID, authorized: Bool) async throws -> TradeDeviceAuthorization {
        struct Request: Encodable { let enabled: Bool }
        struct Response: Decodable { let authorization: TradeDeviceAuthorization }
        let response: Response = try await client.request("POST", path: "\(Self.tradeDevicesPath)/\(deviceID.uuidString)",
            body: WireJSON.encoder().encode(Request(enabled: authorized)))
        return response.authorization
    }
    func health() async throws -> PaperHealth {
        try await client.request("GET", path: "health", authenticated: false)
    }
    func paperBrokerHealth() async throws -> PaperBrokerHealth {
        struct Response: Decodable { let broker: PaperBrokerHealth }
        let response: Response = try await client.request("GET", path: "broker/health")
        return response.broker
    }
    func paperOrders() async throws -> [PaperOrder] {
        struct Response: Decodable { let orders: [PaperOrder] }
        let response: Response = try await client.request("GET", path: "orders")
        return response.orders
    }
    func paperOrder(_ id: UUID) async throws -> PaperOrderDetail {
        try await client.request("GET", path: "orders/\(id.uuidString)")
    }
}
