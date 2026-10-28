#if os(macOS)
import Foundation
import YoloAgentsSDK

public struct PaperOrderDraftRequest: Encodable, Equatable, Sendable {
    public let clientRequestID: UUID
    public let accountSelection: PaperAccountSelection
    public let contract: PaperStockContract
    public let side: PaperOrderSide
    public let quantity: Int
    public let limitPrice: String
    private let orderType = "LMT"
    private let tif = "DAY"
    public init(clientRequestID: UUID, accountSelection: PaperAccountSelection, symbol: String,
                side: PaperOrderSide, quantity: Int, limitPrice: String) throws {
        guard (1...100_000).contains(quantity), PaperOrder.validLimitPrice(limitPrice) else {
            throw PaperOrderValidationError.invalidInput
        }
        self.clientRequestID = clientRequestID; self.accountSelection = accountSelection
        contract = try PaperStockContract(symbol: symbol); self.side = side; self.quantity = quantity; self.limitPrice = limitPrice
    }
    enum CodingKeys: String, CodingKey {
        case contract, side, quantity, tif
        case clientRequestID = "client_request_id", accountSelection = "account_selection"
        case limitPrice = "limit_price", orderType = "order_type"
    }
}

public struct PaperOrderPreviewRequest: Encodable, Equatable, Sendable {
    public let clientRequestID: UUID
    public let version: Int
    public let contentHash: OrderContentHash
    public init(clientRequestID: UUID, order: PaperOrder) throws {
        guard order.hasSupportedShape, [.draft, .proposed].contains(order.state), order.confirmation == nil else { throw PaperOrderValidationError.invalidState }
        self.clientRequestID = clientRequestID; version = order.version; contentHash = order.contentHash
    }
    enum CodingKeys: String, CodingKey { case version; case clientRequestID = "client_request_id", contentHash = "content_hash" }
}
public struct PaperOrderConfirmRequest: Encodable, Equatable, Sendable {
    public let clientRequestID: UUID
    public let version: Int
    public let contentHash: OrderContentHash
    public let previewID: UUID
    public let expiresAt: OrderExpiry
    private let userAction = "confirm_paper_order"
    public init(clientRequestID: UUID, order: PaperOrder, now: Date = Date()) throws {
        guard order.state == .proposed, order.confirmation == nil, let preview = order.preview else { throw PaperOrderValidationError.invalidState }
        guard order.hasValidPreview(at: now) else { throw PaperOrderValidationError.expired }
        self.clientRequestID = clientRequestID; version = order.version; contentHash = order.contentHash
        previewID = preview.id; expiresAt = preview.expiresAt
    }
    enum CodingKeys: String, CodingKey {
        case version
        case clientRequestID = "client_request_id", contentHash = "content_hash", previewID = "preview_id"
        case expiresAt = "expires_at", userAction = "user_action"
    }
}
public struct PaperOrderSubmitRequest: Encodable, Equatable, Sendable {
    public let clientRequestID: UUID
    public let version: Int
    public let contentHash: OrderContentHash
    public let confirmationID: UUID
    private let userAction = "submit_paper_order"
    public init(clientRequestID: UUID, order: PaperOrder, now: Date = Date()) throws {
        guard order.hasSupportedShape, order.state == .confirmed, let confirmation = order.confirmation,
              confirmation.version == order.version, confirmation.contentHash == order.contentHash,
              confirmation.expiresAt == order.preview?.expiresAt else { throw PaperOrderValidationError.invalidState }
        guard order.hasValidPreview(at: now), confirmation.expiresAt.date > now else { throw PaperOrderValidationError.expired }
        self.clientRequestID = clientRequestID; version = order.version; contentHash = order.contentHash; confirmationID = confirmation.id
    }
    enum CodingKeys: String, CodingKey {
        case version
        case clientRequestID = "client_request_id", contentHash = "content_hash", confirmationID = "confirmation_id", userAction = "user_action"
    }
}
public struct PaperOrderCancelRequest: Encodable, Equatable, Sendable {
    public let clientRequestID: UUID
    public let version: Int
    public let contentHash: OrderContentHash
    private let userAction = "cancel_paper_order"
    public init(clientRequestID: UUID, order: PaperOrder) throws {
        guard order.hasSupportedShape, order.state.canCancel, order.filledQuantity < order.quantity else { throw PaperOrderValidationError.invalidState }
        self.clientRequestID = clientRequestID; version = order.version; contentHash = order.contentHash
    }
    enum CodingKeys: String, CodingKey { case version; case clientRequestID = "client_request_id", contentHash = "content_hash", userAction = "user_action" }
}

/// Intentionally absent from iOS builds. The service still reauthenticates every mutation.
public struct PaperOrderWriter: Sendable {
    private let client: APIClient
    public init(connection: PairedConnection, transport: any HTTPTransport = URLSessionTransport()) throws {
        guard connection.agentID == nil || connection.agentID == "trading-agent" else { throw ClientError.agentIdentityMismatch }
        guard connection.pairing.device.platform == .macos, connection.pairing.device.revokedAt == nil,
              connection.pairing.device.permissions.contains("devices:manage") else { throw PaperOrderValidationError.permissionDenied }
        client = APIClient(endpoint: connection.endpoint, token: connection.pairing.token, transport: transport)
    }
    public func createDraft(_ request: PaperOrderDraftRequest) async throws -> PaperOrder {
        try await post(path: "orders", request: request)
    }
    public func preview(_ orderID: UUID, request: PaperOrderPreviewRequest) async throws -> PaperOrder {
        try await post(path: "orders/\(orderID.uuidString)/preview", request: request)
    }
    public func confirm(_ orderID: UUID, request: PaperOrderConfirmRequest) async throws -> PaperOrder {
        try await post(path: "orders/\(orderID.uuidString)/confirm", request: request)
    }
    public func submit(_ orderID: UUID, request: PaperOrderSubmitRequest) async throws -> PaperOrder {
        try await post(path: "orders/\(orderID.uuidString)/submit", request: request)
    }
    public func cancel(_ orderID: UUID, request: PaperOrderCancelRequest) async throws -> PaperOrder {
        try await post(path: "orders/\(orderID.uuidString)/cancel", request: request)
    }
    private func post<Request: Encodable>(path: String, request: Request) async throws -> PaperOrder {
        let response: PaperOrderResponse = try await client.request("POST", path: path, body: WireJSON.encoder().encode(request))
        return response.order
    }
}
#endif
