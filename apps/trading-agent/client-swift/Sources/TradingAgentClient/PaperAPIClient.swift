import Foundation
import YoloAgentsSDK

public struct PaperHealth: Codable, Equatable, Sendable {
    public let status: String
    public let apiVersion: Int
    public let serverID: UUID
    public let agentMode: String
    public let model: String?
    public let tradeExecutionEnabled: Bool
    public let broker: PaperBrokerHealth?
    enum CodingKeys: String, CodingKey {
        case status, model, broker
        case apiVersion = "api_version", serverID = "server_id"
        case agentMode = "agent_mode", tradeExecutionEnabled = "trade_execution_enabled"
    }
}


struct PaperAPIClient: Sendable {
    private let client: APIClient
    init(endpoint: ServiceEndpoint, token: String?, transport: any HTTPTransport = URLSessionTransport()) {
        client = APIClient(endpoint: endpoint, token: token, transport: transport)
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
