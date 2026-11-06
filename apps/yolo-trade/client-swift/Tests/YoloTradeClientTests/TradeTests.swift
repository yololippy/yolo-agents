import Foundation
import Testing
import YoloAgentsSDK
@testable import YoloTradeClient

private let tradeServer = UUID(uuidString: "40000000-0000-4000-8000-000000000004")!
private func tradeDecode<T: Decodable>(_ type: T.Type, _ value: Any) throws -> T {
    try WireJSON.decoder().decode(type, from: JSONSerialization.data(withJSONObject: value))
}
private func instrument(_ symbol: String = "AAPL", id: Int = 265598) -> [String: Any] {
    ["con_id": id, "symbol": symbol, "name": "Apple", "sec_type": "STK", "exchange": "SMART", "primary_exchange": "NASDAQ", "currency": "USD"]
}
private func tradeConnection() throws -> PairedConnection {
    let response = try tradeDecode(PairingResponse.self, [
        "device": ["id": UUID().uuidString, "name": "iPhone", "platform": "ios", "permissions": ["chat:read"], "created_at": "2026-10-28T00:00:00Z"],
        "token": "fixture-token", "server_id": tradeServer.uuidString, "api_version": 1])
    return PairedConnection(endpoint: try ServiceEndpoint("https://trade.example.test"), pairing: response)
}

private actor TradeHTTP: HTTPTransport {
    var requests: [URLRequest] = []
    private var oldReply: CheckedContinuation<HTTPResult, Never>?
    private var quoteReply: CheckedContinuation<HTTPResult, Never>?
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        let path = request.url!.path
        if path == "/v1/health" { return try reply(["status": "ok", "api_version": 1, "server_id": tradeServer.uuidString,
            "agent_mode": "mock", "trade_execution_enabled": false, "agent_id": "yolo-trade"]) }
        if path == "/v1/positions" { return try reply(["positions": [instrument()], "count": 1, "as_of": "2026-10-28T00:00:00Z"]) }
        if path == "/v1/instruments/search" {
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value
            if query == "old" { return await withCheckedContinuation { oldReply = $0 } }
            return try reply(["instruments": (1...20).map { instrument("NEW", id: $0) }])
        }
        if path == "/v1/instruments/265598/quote" { return await withCheckedContinuation { quoteReply = $0 } }
        if path.hasSuffix("/quote") { return try reply(["quote": ["con_id": 2, "bid": "10", "ask": "11", "last": NSNull(), "data_type": "delayed", "as_of": NSNull()]]) }
        throw URLError(.badURL)
    }
    func finishOldSearch() throws { oldReply?.resume(returning: try reply(["instruments": [instrument("OLD")]])); oldReply = nil }
    func finishOldQuote() throws { quoteReply?.resume(returning: try reply(["quote": ["con_id": 265598, "data_type": "live"]])); quoteReply = nil }
    var searchCount: Int { requests.filter { $0.url?.path == "/v1/instruments/search" }.count }
    var waitingForOldSearch: Bool { oldReply != nil }
    var waitingForOldQuote: Bool { quoteReply != nil }
    private func reply(_ value: Any) throws -> HTTPResult { HTTPResult(data: try JSONSerialization.data(withJSONObject: value), status: 200) }
}

@Test func tradeReadModelsKeepMissingValuesAndUnsupportedAssetsReadable() throws {
    let position = try tradeDecode(TradePosition.self, instrument())
    #expect(position.quantity == nil && position.averageCost == nil && position.marketPrice == nil)
    #expect(position.marketValue == nil && position.unrealizedPNL == nil && position.asOf == nil)
    #expect(position.contract.quickContract?.conID == 265598)
    for field in ["sec_type", "exchange", "currency", "primary_exchange"] {
        var value = instrument(); value[field] = "OTHER"
        if field == "primary_exchange" { value.removeValue(forKey: field) }
        let asset = try tradeDecode(TradeInstrument.self, value)
        #expect(asset.quickContract == nil)
    }
    for field in ["con_id", "symbol", "sec_type", "currency"] {
        var value = instrument(); value.removeValue(forKey: field)
        #expect(throws: (any Error).self) { try tradeDecode(TradeInstrument.self, value) }
    }
    var badID = instrument(); badID["con_id"] = -1
    #expect(throws: (any Error).self) { try tradeDecode(TradeInstrument.self, badID) }
    var wrongNumber = instrument(); wrongNumber["quantity"] = 1
    #expect(throws: (any Error).self) { try tradeDecode(TradePosition.self, wrongNumber) }
}

@Test func exactContractsAndManualPriceFailClosed() throws {
    let contract = try #require(try tradeDecode(TradeInstrument.self, instrument()).quickContract)
    var ticket = PaperQuickTicket(contract: contract, side: .buy)
    #expect(ticket.limitPrice.isEmpty && ticket.quantity.isEmpty && !ticket.isValid)
    ticket.quantity = "2"
    #expect(!ticket.isValid)
    ticket.limitPrice = "100.1250"
    #expect(ticket.isValid)
    let object = try #require(JSONSerialization.jsonObject(with: WireJSON.encoder().encode(contract)) as? [String: Any])
    #expect(object["con_id"] as? Int == 265598 && object["primary_exchange"] as? String == "NASDAQ")
    var partial = object; partial.removeValue(forKey: "primary_exchange")
    #expect(throws: (any Error).self) { try tradeDecode(PaperStockContract.self, partial) }
    let different = try tradeDecode(QualifiedPaperContract.self, instrument(id: 123))
    #expect(!different.matches(contract))
    var wrongVenue = instrument(); wrongVenue["primary_exchange"] = "NYSE"
    #expect(try !tradeDecode(QualifiedPaperContract.self, wrongVenue).matches(contract))
}

@Test func tradeAuthorizationUsesTheActualHostWireContract() throws {
    let deviceID = UUID()
    let value: [String: Any] = ["device_id": deviceID.uuidString, "paper_trading_enabled": true, "authorization_revision": "opaque-revision-one"]
    #expect(try tradeDecode(TradeDeviceAuthorization.self, value).permitsPaper)
    for field in ["device_id", "paper_trading_enabled"] {
        var missing = value; missing.removeValue(forKey: field)
        #expect(throws: (any Error).self) { try tradeDecode(TradeDeviceAuthorization.self, missing) }
    }
    #expect(try !tradeDecode(TradeDeviceAuthorization.self, ["device_id": deviceID.uuidString, "paper_trading_enabled": false]).permitsPaper)
    #expect(throws: (any Error).self) { try tradeDecode(TradeDeviceAuthorization.self, ["device_id": deviceID.uuidString, "paper_trading_enabled": "true"]) }
    let phone = try tradeDecode(TradePairedPhone.self, ["device_id": deviceID.uuidString, "device_name": "Phone", "revoked_at": "2026-10-28T00:00:00Z", "paper_trading_enabled": true, "authorization_revision": "opaque-revision-one"])
    #expect(!phone.permitsPaper && phone.revokedAt != nil)
}

@Test @MainActor func searchDebouncesAndCapsResults() async throws {
    let http = TradeHTTP()
    let model = TradeModel(connection: try tradeConnection(), transport: http, debounce: .milliseconds(30))
    model.search("a"); model.search("aa"); model.search("Apple & Co")
    try await Task.sleep(for: .milliseconds(150))
    #expect(await http.searchCount == 1)
    #expect(model.results.count == 16 && !model.isSearching)
    let requests = await http.requests
    let request = try #require(requests.last)
    #expect(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first?.value == "Apple & Co")
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-token")
    model.search("   ")
    #expect(model.results.isEmpty && !model.isSearching)
}

@Test @MainActor func oldSearchAndQuoteCannotReplaceNewSelection() async throws {
    let http = TradeHTTP()
    let model = TradeModel(connection: try tradeConnection(), transport: http, debounce: .zero)
    model.search("old")
    for _ in 0..<200 { if await http.waitingForOldSearch { break }; try await Task.sleep(for: .milliseconds(2)) }
    #expect(await http.waitingForOldSearch)
    model.search("new")
    for _ in 0..<200 { if !model.results.isEmpty { break }; try await Task.sleep(for: .milliseconds(2)) }
    try await http.finishOldSearch()
    await Task.yield()
    #expect(model.results.first?.symbol == "NEW")
    let old = try tradeDecode(TradeInstrument.self, instrument())
    let newer = try tradeDecode(TradeInstrument.self, instrument("NEW", id: 2))
    let task = Task { await model.select(old) }
    for _ in 0..<200 { if await http.waitingForOldQuote { break }; try await Task.sleep(for: .milliseconds(2)) }
    await model.select(newer)
    try await http.finishOldQuote(); await task.value
    #expect(model.selected?.conID == 2 && model.quote?.conID == 2 && model.quote?.last == nil)
    #expect(model.quote?.dataType == "delayed" && model.quote?.asOf == nil)
    await model.refreshPositions()
    #expect(model.positions.count == 1)
    #expect(await http.requests.allSatisfy { $0.httpMethod == "GET" })
}

@Test @MainActor func tradeNavigationExposesTheSameThreePagesOnBothPlatforms() throws {
    #expect(TradePage.allCases.map(\.title) == ["持仓", "搜索", "订单"])
    let view = YoloTradeRootView.paperOrdersPage(connection: try tradeConnection())
    #if os(iOS)
    #expect(view.presentation == .page)
    #else
    #expect(view.presentation == .modal)
    #endif
}

private actor TradeManagementHTTP: HTTPTransport {
    let managerID: UUID
    let phoneID = UUID()
    var requests: [URLRequest] = []
    var authorized = false
    init(managerID: UUID) { self.managerID = managerID }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        let value: [String: Any]
        if request.url?.path == "/v1/health" {
            value = ["status": "ok", "api_version": 1, "server_id": tradeServer.uuidString, "agent_mode": "mock", "trade_execution_enabled": false, "agent_id": "yolo-trade"]
        } else if request.url?.path == "/v1/broker/health" { value = ["broker": [:]] }
        else if request.url?.path == "/v1/paper-trading/authorization" {
            value = ["authorization": ["device_id": managerID.uuidString, "paper_trading_enabled": true, "authorization_revision": NSNull()]]
        } else if request.httpMethod == "POST" {
            let body = try JSONSerialization.jsonObject(with: #require(request.httpBody)) as? [String: Bool]
            authorized = body?["enabled"] == true
            value = ["authorization": ["device_id": phoneID.uuidString, "paper_trading_enabled": authorized, "authorization_revision": authorized ? "fixture-revision" as Any : NSNull()]]
        } else {
            value = ["devices": [["device_id": phoneID.uuidString, "device_name": "Phone", "revoked_at": NSNull(), "paper_trading_enabled": authorized, "authorization_revision": authorized ? "fixture-revision" as Any : NSNull()]]]
        }
        return HTTPResult(data: try JSONSerialization.data(withJSONObject: value), status: 200)
    }
    var posts: [URLRequest] { requests.filter { $0.httpMethod == "POST" } }
}

@Test @MainActor func onlyMacManagerCanAuthorizeKnownPairedPhonesAndRevoke() async throws {
    let managerID = UUID()
    let pairing = try tradeDecode(PairingResponse.self, [
        "device": ["id": managerID.uuidString, "name": "Mac", "platform": "macos", "permissions": ["devices:manage"], "created_at": "2026-10-28T00:00:00Z"],
        "token": "fixture-token", "server_id": tradeServer.uuidString, "api_version": 1])
    let connection = PairedConnection(endpoint: try ServiceEndpoint("https://trade.example.test"), pairing: pairing)
    let http = TradeManagementHTTP(managerID: managerID)
    let model = PaperOrdersModel(connection: connection, platform: .macos, transport: http)
    try await model.refreshDeviceAuthorization()
    #expect(model.isDeviceManager && model.tradingDevices.count == 1)
    await model.setDeviceAuthorization(UUID(), authorized: true)
    let rejectedPosts = await http.posts
    #expect(model.actionError != nil && rejectedPosts.isEmpty)
    let phoneID = http.phoneID
    await model.setDeviceAuthorization(phoneID, authorized: true)
    #expect(model.tradingDevices.first?.permitsPaper == true)
    await model.setDeviceAuthorization(phoneID, authorized: false)
    #expect(model.tradingDevices.first?.permitsPaper == false)
    let posts = await http.posts
    #expect(posts.map { $0.url!.path } == ["/v1/paper-trading/devices/\(phoneID.uuidString)", "/v1/paper-trading/devices/\(phoneID.uuidString)"])
    let bodies = try posts.map { try JSONSerialization.jsonObject(with: #require($0.httpBody)) as? [String: Bool] }
    #expect(bodies == [["enabled": true], ["enabled": false]])
    let phone = PaperOrdersModel(connection: try tradeConnection(), platform: .ios, transport: http)
    await phone.setDeviceAuthorization(phoneID, authorized: true)
    let finalPosts = await http.posts
    #expect(!phone.isDeviceManager && finalPosts.count == 2)
}

@Test func flatPositionsEnvelopeKeepsTopLevelAsOfAndAllAssets() throws {
    let response = try tradeDecode(TradePositionsResponse.self, ["positions": [[
        "con_id": 42, "symbol": "US TREASURY", "sec_type": "BOND", "currency": "USD", "exchange": NSNull(),
        "primary_exchange": NSNull(), "quantity": "5", "average_cost": "99.5", "market_price": NSNull(),
        "market_value": NSNull(), "unrealized_pnl": NSNull(), "realized_pnl": "0"]], "count": 1, "as_of": "2026-10-28T00:00:00Z"])
    #expect(response.count == 1 && response.positions.first?.contract.secType == "BOND")
    #expect(response.positions.first?.quantity == "5" && response.positions.first?.contract.quickContract == nil)
    #expect(ISO8601DateFormatter().string(from: response.asOf) == "2026-10-28T00:00:00Z")
}

@Test func actualRiskReportRetainsQuoteContextAndMetrics() throws {
    let risk = try tradeDecode(PaperRiskReport.self, ["status": "approved", "policy": ["policy_id": "fixture"],
        "evaluated_at": "2026-10-28T00:00:00Z", "market_data_type": "delayed", "monetary_limits_applied": true,
        "metrics": ["order_notional_usd": "200.25", "projected_gross_exposure_usd": "1200.25", "projected_gross_exposure_limit_usd": "20000",
                    "symbol_concentration_percent": "2", "daily_submitted_notional_usd": "200.25", "quote_deviation_percent": "0.1", "reference_price": "100"]])
    #expect(risk.status == "approved" && risk.marketDataType == "delayed" && risk.monetaryLimitsApplied)
    #expect(risk.metrics.orderNotional == "200.25" && risk.metrics.quoteDeviationPercent == "0.1")
}

@Test func phoneWriterRequiresEnabledNonemptyRevisionAndMacAcceptsNull() throws {
    let connection = try tradeConnection()
    let id = connection.pairing.device.id
    for revision in [NSNull() as Any, "", "  "] {
        let authorization = try tradeDecode(TradeDeviceAuthorization.self, ["device_id": id.uuidString,
            "paper_trading_enabled": true, "authorization_revision": revision])
        #expect(authorization.permitsPaper && !authorization.permitsPhonePaper)
        #expect(throws: PaperOrderValidationError.permissionDenied) {
            try PaperOrderWriter(connection: connection, authorization: authorization)
        }
    }
    let missing = try tradeDecode(TradeDeviceAuthorization.self, ["device_id": id.uuidString, "paper_trading_enabled": true])
    #expect(throws: PaperOrderValidationError.permissionDenied) { try PaperOrderWriter(connection: connection, authorization: missing) }
    let valid = try tradeDecode(TradeDeviceAuthorization.self, ["device_id": id.uuidString, "paper_trading_enabled": true, "authorization_revision": "opaque-revision-one"])
    _ = try PaperOrderWriter(connection: connection, authorization: valid)
    #expect(valid.authorizationRevision == "opaque-revision-one")
    let disabled = try tradeDecode(TradeDeviceAuthorization.self, ["device_id": id.uuidString, "paper_trading_enabled": false, "authorization_revision": "opaque-revision-one"])
    #expect(throws: PaperOrderValidationError.permissionDenied) { try PaperOrderWriter(connection: connection, authorization: disabled) }
    #expect(throws: (any Error).self) {
        try tradeDecode(TradeDeviceAuthorization.self, ["device_id": id.uuidString, "paper_trading_enabled": true, "authorization_revision": 7])
    }
    let noRevisionPhone = try tradeDecode(TradePairedPhone.self, ["device_id": id.uuidString, "device_name": "Phone", "revoked_at": NSNull(), "paper_trading_enabled": true, "authorization_revision": NSNull()])
    #expect(!noRevisionPhone.permitsPaper)
    let listedPhone = try tradeDecode(TradePairedPhone.self, ["device_id": id.uuidString, "device_name": "Phone", "revoked_at": NSNull(), "paper_trading_enabled": true, "authorization_revision": "opaque-revision-one"])
    #expect(listedPhone.permitsPaper && listedPhone.authorizationRevision == valid.authorizationRevision)
    let macPairing = try tradeDecode(PairingResponse.self, [
        "device": ["id": id.uuidString, "name": "Mac", "platform": "macos", "permissions": ["devices:manage"], "created_at": "2026-10-28T00:00:00Z"],
        "token": "fixture-token", "server_id": tradeServer.uuidString, "api_version": 1])
    let mac = PairedConnection(endpoint: connection.endpoint, pairing: macPairing)
    let macAuthorization = try tradeDecode(TradeDeviceAuthorization.self, ["device_id": id.uuidString,
        "paper_trading_enabled": true, "authorization_revision": NSNull()])
    #expect(macAuthorization.permitsPaper && macAuthorization.authorizationRevision == nil)
    _ = try PaperOrderWriter(connection: mac, authorization: macAuthorization)
}
