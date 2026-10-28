import Foundation
import Testing
import YoloAgentsSDK
@testable import TradingAgentClient

private let paperConfig = String(repeating: "a", count: 64)
private let paperHash = String(repeating: "b", count: 64)
private let paperNow = Date(timeIntervalSince1970: 1_800_000_000)
private let paperExpiry = "2030-01-01T00:00:00.123456Z"
private let paperOrderID = UUID(uuidString: "10000000-0000-4000-8000-000000000001")!
private let paperServerID = UUID(uuidString: "40000000-0000-4000-8000-000000000004")!

private func paperBrokerObject() -> [String: Any] {
    ["configuration": ["config_id": paperConfig, "environment": "paper", "account_configured": true,
                       "read_only": false, "execution_enabled": true],
     "state": "connected", "connected": true, "execution_enabled": true, "environment_verified": true,
     "data_complete": true, "account_verified": true, "permissions_verified": true,
     "health_stale": false, "session_id": "fixture-session", "blockers": []]
}
private func paperHealthObject(broker: [String: Any]? = paperBrokerObject()) -> [String: Any] {
    var value: [String: Any] = ["status": "ok", "api_version": 1, "server_id": paperServerID.uuidString,
                               "agent_mode": "mock", "trade_execution_enabled": true]
    value["broker"] = broker
    return value
}
private func paperOrderObject(state: String = "proposed") -> [String: Any] {
    var value: [String: Any] = [
        "id": paperOrderID.uuidString, "client_request_id": UUID().uuidString, "environment": "paper",
        "account_selection": paperConfig, "contract": ["symbol": "AAPL", "sec_type": "STK", "exchange": "SMART", "currency": "USD"],
        "side": "BUY", "quantity": 2, "limit_price": "100.1250", "order_type": "LMT", "tif": "DAY",
        "version": 2, "content_hash": paperHash, "state": state, "filled_quantity": 0,
        "created_at": "2026-10-28T00:00:00Z", "updated_at": "2026-10-28T00:00:01Z",
    ]
    if state != "draft" {
        value["preview"] = [
            "id": "20000000-0000-4000-8000-000000000002",
            "qualified_contract": ["con_id": 265598, "symbol": "AAPL", "sec_type": "STK", "exchange": "SMART", "currency": "USD"],
            "what_if": ["complete": true, "accepted": true, "currency": "USD", "initial_margin_change": "100.25",
                        "maintenance_margin_change": "-10.00", "commission": "1.00"],
            "expires_at": paperExpiry,
        ] as [String: Any]
    }
    if state == "confirmed" {
        value["confirmation"] = ["id": "30000000-0000-4000-8000-000000000003", "version": 2,
                                 "content_hash": paperHash, "expires_at": paperExpiry] as [String: Any]
    }
    return value
}
private func paperDecode<T: Decodable>(_ type: T.Type, _ object: Any) throws -> T {
    try WireJSON.decoder().decode(type, from: JSONSerialization.data(withJSONObject: object, options: [.fragmentsAllowed]))
}
private func paperBody<T: Encodable>(_ value: T) throws -> [String: Any] {
    try #require(JSONSerialization.jsonObject(with: WireJSON.encoder().encode(value)) as? [String: Any])
}
private func paperConnection(platform: DevicePlatform = .macos, permissions: [String] = ["devices:manage"], revoked: Bool = false) throws -> PairedConnection {
    let response = try paperDecode(PairingResponse.self, [
        "device": ["id": UUID().uuidString, "name": "Fixture", "platform": platform.rawValue,
                   "permissions": permissions, "created_at": "2026-10-28T00:00:00Z",
                   "revoked_at": revoked ? "2026-10-28T01:00:00Z" : NSNull()] as [String: Any],
        "token": "fixture-token", "server_id": paperServerID.uuidString, "api_version": 1,
    ])
    return PairedConnection(endpoint: try ServiceEndpoint("https://paper.example.test"), pairing: response)
}
private actor PaperRecordingHTTP: HTTPTransport {
    let replies: [HTTPResult]
    private(set) var requests: [URLRequest] = []
    init(_ replies: [HTTPResult] = []) { self.replies = replies }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        guard requests.count <= replies.count else { throw URLError(.timedOut) }
        return replies[requests.count - 1]
    }
}

@Test func paperTargetComesOnlyFromConfiguredPaperHealth() throws {
    let health = try paperDecode(PaperHealth.self, paperHealthObject())
    #expect(try PaperAccountSelection(health: health).rawValue == paperConfig)
    #expect(health.paperExecutionReady)
    for config in [
        ["config_id": paperConfig, "environment": "live", "account_configured": true],
        ["config_id": "DU12345", "environment": "paper", "account_configured": true],
        ["config_id": paperConfig, "environment": "paper", "account_configured": false],
        ["environment": "paper", "account_configured": true],
    ] as [[String: Any]] {
        var broker = paperBrokerObject(); broker["configuration"] = config
        let unavailable = try paperDecode(PaperHealth.self, paperHealthObject(broker: broker))
        #expect(!unavailable.paperExecutionReady)
        #expect(throws: PaperOrderValidationError.targetUnavailable) { try PaperAccountSelection(health: unavailable) }
    }
    let oldHealth = try paperDecode(PaperHealth.self, paperHealthObject(broker: nil))
    #expect(oldHealth.broker == nil && !oldHealth.paperExecutionReady)
}

@Test(arguments: ["connected", "execution_enabled", "environment_verified", "data_complete", "account_verified", "permissions_verified", "health_stale", "session_id"])
func paperExecutionFailsClosedForEveryMissingGate(_ field: String) throws {
    var broker = paperBrokerObject(); broker.removeValue(forKey: field)
    #expect(try !paperDecode(PaperHealth.self, paperHealthObject(broker: broker)).paperExecutionReady)
}

@Test(arguments: ["2030-01-01T00:00:00.123456Z", "2030-01-01T08:00:00.123456+08:00"])
func paperExpiryRetainsExactWireSpelling(_ expiry: String) throws {
    let decoded = try paperDecode(OrderExpiry.self, expiry)
    let encoded = try WireJSON.encoder().encode(decoded)
    #expect(try JSONDecoder().decode(String.self, from: encoded) == expiry)
}

@Test func paperReadAPIAuthenticatesAndPreservesUnknownStates() async throws {
    let order = paperOrderObject(state: "future_status")
    let replies: [Any] = [["broker": paperBrokerObject()], ["orders": [order]],
        ["order": order, "events": [["seq": 1, "type": "order.future_status", "state": "future_status", "created_at": "2026-10-28T00:00:00Z"]]]]
    let http = try PaperRecordingHTTP(replies.map { HTTPResult(data: try JSONSerialization.data(withJSONObject: $0), status: 200) })
    let connection = try paperConnection(platform: .ios)
    let api = PaperAPIClient(endpoint: connection.endpoint, token: connection.pairing.token, transport: http)
    _ = try await api.paperBrokerHealth()
    #expect(try await api.paperOrders().first?.state.rawValue == "future_status")
    let detail = try await api.paperOrder(paperOrderID)
    #expect(!detail.order.state.canCancel && detail.events.first?.state?.rawValue == "future_status")
    let requests = await http.requests
    #expect(requests.map(\.httpMethod) == ["GET", "GET", "GET"])
    #expect(requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-token" && $0.url?.query == nil })
    #expect(requests.last?.url?.path == "/v1/orders/\(paperOrderID.uuidString)")
}

#if os(macOS)
@Test func paperDraftEncodesExactTypedFieldsAndNormalizesSymbol() throws {
    let selection = try PaperAccountSelection(health: paperDecode(PaperHealth.self, paperHealthObject()))
    let id = UUID()
    let request = try PaperOrderDraftRequest(clientRequestID: id, accountSelection: selection, symbol: " aapl ", side: .sell, quantity: 2, limitPrice: "100.1250")
    let body = try paperBody(request)
    #expect(Set(body.keys) == ["client_request_id", "account_selection", "contract", "side", "quantity", "limit_price", "order_type", "tif"])
    #expect(body["quantity"] as? Int == 2 && body["limit_price"] as? String == "100.1250")
    #expect(body["client_request_id"] as? String == id.uuidString && body["account_selection"] as? String == paperConfig)
    #expect(body["side"] as? String == "SELL" && body["order_type"] as? String == "LMT" && body["tif"] as? String == "DAY")
    #expect(body["contract"] as? [String: String] == ["symbol": "AAPL", "sec_type": "STK", "exchange": "SMART", "currency": "USD"])
}

@Test(arguments: ["0", "-1", "NaN", "1e2", "1.00001", "1000000.0001", "01", ".1", "1 "])
func paperDraftRejectsInvalidPrices(_ price: String) throws {
    let selection = try PaperAccountSelection(health: paperDecode(PaperHealth.self, paperHealthObject()))
    #expect(throws: PaperOrderValidationError.invalidInput) {
        try PaperOrderDraftRequest(clientRequestID: UUID(), accountSelection: selection, symbol: "AAPL", side: .buy, quantity: 1, limitPrice: price)
    }
}

@Test func paperMutationBindingsHaveExactKeysAndExplicitActions() throws {
    let draft = try paperDecode(PaperOrder.self, paperOrderObject(state: "draft"))
    let proposed = try paperDecode(PaperOrder.self, paperOrderObject())
    let confirmed = try paperDecode(PaperOrder.self, paperOrderObject(state: "confirmed"))
    let working = try paperDecode(PaperOrder.self, paperOrderObject(state: "working"))
    let preview = try paperBody(PaperOrderPreviewRequest(clientRequestID: UUID(), order: draft))
    let confirm = try paperBody(PaperOrderConfirmRequest(clientRequestID: UUID(), order: proposed, now: paperNow))
    let submit = try paperBody(PaperOrderSubmitRequest(clientRequestID: UUID(), order: confirmed, now: paperNow))
    let cancel = try paperBody(PaperOrderCancelRequest(clientRequestID: UUID(), order: working))
    #expect(Set(preview.keys) == ["client_request_id", "version", "content_hash"])
    #expect(Set(confirm.keys) == ["client_request_id", "version", "content_hash", "preview_id", "expires_at", "user_action"])
    #expect(Set(submit.keys) == ["client_request_id", "version", "content_hash", "confirmation_id", "user_action"])
    #expect(Set(cancel.keys) == ["client_request_id", "version", "content_hash", "user_action"])
    #expect(confirm["expires_at"] as? String == paperExpiry && confirm["user_action"] as? String == "confirm_paper_order")
    #expect(submit["user_action"] as? String == "submit_paper_order" && cancel["user_action"] as? String == "cancel_paper_order")
    #expect([preview, confirm, submit, cancel].allSatisfy { $0["version"] as? Int == 2 && $0["content_hash"] as? String == paperHash })
    #expect(Set([preview, confirm, submit, cancel].compactMap { $0["client_request_id"] as? String }).count == 4)
}

@Test func paperInvalidBindingExpiredPreviewAndUnknownCannotExecute() throws {
    for field in ["version", "content_hash", "expires_at"] {
        var object = paperOrderObject(state: "confirmed")
        var binding = try #require(object["confirmation"] as? [String: Any])
        switch field {
        case "version": binding[field] = 1
        case "content_hash": binding[field] = String(repeating: "c", count: 64)
        default: binding[field] = "2030-01-01T00:00:00.123Z"
        }
        object["confirmation"] = binding
        let order = try paperDecode(PaperOrder.self, object)
        #expect(throws: PaperOrderValidationError.invalidState) { try PaperOrderSubmitRequest(clientRequestID: UUID(), order: order, now: paperNow) }
    }
    let proposed = try paperDecode(PaperOrder.self, paperOrderObject())
    #expect(throws: PaperOrderValidationError.expired) {
        try PaperOrderConfirmRequest(clientRequestID: UUID(), order: proposed, now: Date(timeIntervalSince1970: 2_000_000_000))
    }
    for state in ["unknown", "submitting", "cancel_pending", "filled", "future_status"] {
        let order = try paperDecode(PaperOrder.self, paperOrderObject(state: state))
        #expect(throws: PaperOrderValidationError.invalidState) { try PaperOrderCancelRequest(clientRequestID: UUID(), order: order) }
        #expect(throws: PaperOrderValidationError.invalidState) { try PaperOrderSubmitRequest(clientRequestID: UUID(), order: order, now: paperNow) }
    }
}

@Test func paperWriterRejectsIOSRevokedAndUnprivilegedConnections() throws {
    for connection in [try paperConnection(platform: .ios), try paperConnection(permissions: ["chat:read"]), try paperConnection(revoked: true)] {
        #expect(throws: PaperOrderValidationError.permissionDenied) { try PaperOrderWriter(connection: connection) }
    }
}

@Test func paperExplicitReplayRetainsRequestIdentityAndTransportNeverRetries() async throws {
    let order = try paperDecode(PaperOrder.self, paperOrderObject(state: "draft"))
    let body = try JSONSerialization.data(withJSONObject: ["order": paperOrderObject(state: "draft")])
    let http = PaperRecordingHTTP([HTTPResult(data: body, status: 201), HTTPResult(data: body, status: 200)])
    let writer = try PaperOrderWriter(connection: paperConnection(), transport: http)
    let request = try PaperOrderDraftRequest(clientRequestID: UUID(), accountSelection: order.accountSelection,
        symbol: "AAPL", side: .buy, quantity: 2, limitPrice: "100.1250")
    _ = try await writer.createDraft(request)
    _ = try await writer.createDraft(request)
    let requests = await http.requests
    #expect(requests.count == 2 && requests.allSatisfy { $0.httpMethod == "POST" })
    let first = try #require(requests[0].httpBody)
    let second = try #require(requests[1].httpBody)
    #expect(try JSONSerialization.jsonObject(with: first) as? NSDictionary == JSONSerialization.jsonObject(with: second) as? NSDictionary)
    let timeout = PaperRecordingHTTP()
    let noRetry = try PaperOrderWriter(connection: paperConnection(), transport: timeout)
    await #expect(throws: URLError.self) { try await noRetry.createDraft(request) }
    #expect(await timeout.requests.count == 1)
}
#endif
