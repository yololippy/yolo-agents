import Foundation
import Testing
import YoloAgentsSDK
import YoloAgentsUI
@testable import TradingAgentClient

private let ordersServerID = "40000000-0000-4000-8000-000000000004"
private let ordersID = UUID(uuidString: "10000000-0000-4000-8000-000000000001")!
private let ordersConfig = String(repeating: "a", count: 64)
private let ordersHash = String(repeating: "b", count: 64)
private let ordersNow = Date(timeIntervalSince1970: 1_800_000_000)
private let ordersExpiry = "2030-01-01T00:00:00.123456Z"

private func ordersConnection(platform: DevicePlatform = .macos, privileged: Bool = true) throws -> PairedConnection {
    let body: [String: Any] = [
        "device": ["id": UUID().uuidString, "name": "Fixture", "platform": platform.rawValue,
                   "permissions": privileged && platform == .macos ? ["devices:manage", "chat:read"] : ["chat:read"],
                   "created_at": "2026-10-28T00:00:00Z", "revoked_at": NSNull()],
        "token": "fixture-paper-token", "server_id": ordersServerID, "api_version": 1,
    ]
    let pairing = try WireJSON.decoder().decode(PairingResponse.self, from: JSONSerialization.data(withJSONObject: body))
    return PairedConnection(endpoint: try ServiceEndpoint("https://paper.example.test"), pairing: pairing)
}

private func ordersFixture(state: String = "draft") -> [String: Any] {
    var order: [String: Any] = [
        "id": ordersID.uuidString, "client_request_id": UUID().uuidString, "account_selection": ordersConfig,
        "environment": "paper", "contract": ["symbol": "AAPL", "sec_type": "STK", "exchange": "SMART", "currency": "USD"],
        "side": "BUY", "quantity": 2, "limit_price": "100.1250", "order_type": "LMT", "tif": "DAY",
        "version": state == "draft" ? 1 : 2, "content_hash": ordersHash, "state": state, "filled_quantity": 0,
        "created_at": "2026-10-28T00:00:00Z", "updated_at": "2026-10-28T00:00:00Z",
    ]
    if state != "draft" {
        order["preview"] = [
            "id": "20000000-0000-4000-8000-000000000002",
            "qualified_contract": ["con_id": 265598, "symbol": "AAPL", "sec_type": "STK", "exchange": "SMART", "currency": "USD"],
            "what_if": ["complete": true, "accepted": true, "currency": "USD", "initial_margin_change": "100.25",
                        "maintenance_margin_change": "-10.00", "commission": "1.00"],
            "expires_at": ordersExpiry,
        ] as [String: Any]
    }
    if state == "confirmed" {
        order["confirmation"] = ["id": "30000000-0000-4000-8000-000000000003", "version": 2,
                                 "content_hash": ordersHash, "expires_at": ordersExpiry] as [String: Any]
    }
    return order
}

/// A local state machine only. There is no URLSession, broker, credential store or filesystem access.
private actor OrdersHTTP: HTTPTransport {
    private var order: [String: Any]?
    private var broker: [String: Any] = [
        "configuration": ["config_id": ordersConfig, "environment": "paper", "account_configured": true,
                          "read_only": false, "execution_enabled": true],
        "state": "connected", "connected": true, "execution_enabled": true, "environment_verified": true,
        "data_complete": true, "account_verified": true, "permissions_verified": true,
        "health_stale": false, "session_id": "fixture-session", "blockers": [],
    ]
    private var revision = 0
    private var timeoutAction: String?
    private var corruptAction: String?
    private var protectedSession: String?
    private var healthAgent: String?
    private(set) var requests: [URLRequest] = []
    init(state: String? = nil) { order = state.map { ordersFixture(state: $0) } }
    func failAfter(_ action: String) { timeoutAction = action }
    func corruptResponse(_ action: String) { corruptAction = action }
    func changeProtectedSession() { protectedSession = "different-session" }
    func changeHealthAgent(_ value: String) { healthAgent = value }
    func removeGate(_ key: String) { broker.removeValue(forKey: key) }
    func removeTarget() { broker["configuration"] = ["environment": "paper", "account_configured": true] }
    func setLive() { broker["configuration"] = ["config_id": ordersConfig, "environment": "live", "account_configured": true, "read_only": false, "execution_enabled": true] }
    func setState(_ state: String, filled: Int = 0) {
        order?["state"] = state; order?["filled_quantity"] = filled; tick()
    }
    func changeVersion() { order?["version"] = 9; tick() }
    func incompletePreview() {
        var preview = order?["preview"] as? [String: Any] ?? [:]
        var whatIf = preview["what_if"] as? [String: Any] ?? [:]
        whatIf["complete"] = false; preview["what_if"] = whatIf; order?["preview"] = preview
    }
    func tick() {
        revision += 1
        order?["updated_at"] = String(format: "2026-10-28T00:01:%02dZ", revision)
    }
    var posts: [URLRequest] { requests.filter { $0.httpMethod == "POST" } }
    var actions: [String] { posts.compactMap { $0.url?.lastPathComponent } }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        let path = request.url!.path
        if request.httpMethod == "GET" {
            if path == "/v1/health" {
                var value: [String: Any] = ["status": "ok", "api_version": 1, "server_id": ordersServerID,
                    "agent_mode": "mock", "trade_execution_enabled": true, "broker": broker]
                value["agent_id"] = healthAgent
                return try response(value)
            }
            if path == "/v1/broker/health" {
                var protected = broker
                if let protectedSession { protected["session_id"] = protectedSession }
                return try response(["broker": protected])
            }
            if path == "/v1/orders" { return try response(["orders": order.map { [$0] } ?? []]) }
            guard let order else { throw URLError(.resourceUnavailable) }
            return try response(["order": order, "events": [["seq": revision + 1, "type": "order.updated",
                "state": order["state"]!, "created_at": "2026-10-28T00:01:00Z"]]])
        }
        let data = try #require(request.httpBody)
        let body = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let action = request.url!.lastPathComponent
        switch action {
        case "orders":
            order = ordersFixture()
            order?["client_request_id"] = body["client_request_id"]
        case "preview":
            let originalID = order?["client_request_id"]
            order = ordersFixture(state: "proposed"); order?["client_request_id"] = originalID
        case "confirm":
            let originalID = order?["client_request_id"]
            order = ordersFixture(state: "confirmed"); order?["client_request_id"] = originalID
        case "submit": order?["state"] = timeoutAction == action ? "unknown" : "accepted"
        case "cancel": order?["state"] = "cancel_pending"
        default: throw URLError(.badURL)
        }
        tick()
        if timeoutAction == action { throw URLError(.timedOut) }
        var reply = try #require(order)
        if corruptAction == action { reply["id"] = UUID().uuidString }
        return try response(["order": reply])
    }
    private func response(_ value: [String: Any]) throws -> HTTPResult {
        HTTPResult(data: try JSONSerialization.data(withJSONObject: value), status: 200)
    }
}

@MainActor private func ordersModel(_ http: OrdersHTTP, platform: DevicePlatform = .macos,
                                   privileged: Bool = true, now: Date = ordersNow) throws -> PaperOrdersModel {
    PaperOrdersModel(connection: try ordersConnection(platform: platform, privileged: privileged),
                     platform: platform, transport: http, clock: { now })
}
@MainActor private func selectOrder(_ model: PaperOrdersModel) async {
    model.selectedID = ordersID
    await model.loadSelected()
}

@Test @MainActor func paperHistoryRefreshAndSelectionOnlyRead() async throws {
    let http = OrdersHTTP(state: "unknown")
    let model = try ordersModel(http)
    await model.refresh(); await selectOrder(model); await model.refresh()
    #expect(model.currentOrder?.state == .unknown && model.detail?.events.count == 1)
    #expect(await http.posts.isEmpty)
    #expect(await http.requests.filter { $0.url?.path != "/v1/health" }.allSatisfy {
        $0.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-paper-token"
    })
}

@Test @MainActor func paperRejectsAnotherAgentBeforeSendingDeviceCredentials() async throws {
    let http = OrdersHTTP(state: "draft")
    await http.changeHealthAgent("b-agent")
    let model = try ordersModel(http)
    await model.refresh()
    #expect(model.readError != nil && model.orders.isEmpty)
    #expect(await http.requests.count == 1)
    #expect(await http.requests.first?.value(forHTTPHeaderField: "Authorization") == nil)
}

@Test @MainActor func paperIOSAndUnprivilegedDevicesOnlyReadHistory() async throws {
    for (platform, privileged) in [(DevicePlatform.ios, true), (.macos, false)] {
        let http = OrdersHTTP(state: "proposed")
        let model = try ordersModel(http, platform: platform, privileged: privileged)
        await model.refresh(); await selectOrder(model)
        #expect(!model.canManage && !model.executionReady && model.orders.count == 1)
        #if os(macOS)
        await model.createDraft(symbol: "AAPL", side: .buy, quantity: "2", limitPrice: "100.1250")
        await model.previewCurrentOrder()
        model.reviewConfirmation(); model.reviewSubmission(); model.reviewCancellation()
        await model.performReviewedAction(UUID())
        #expect(model.review == nil)
        #endif
        #expect(await http.posts.isEmpty)
    }
}

#if os(macOS)
@Test @MainActor func paperOneConfirmationContinuesThroughWhatIfConfirmAndSubmit() async throws {
    let http = OrdersHTTP(state: "draft")
    let model = try ordersModel(http)
    await model.refresh(); await selectOrder(model)

    await model.prepareCompleteSubmission()
    let review = try #require(model.review)
    #expect(review.kind == "confirm-and-submit")
    #expect(review.summary.contains("初始保证金变动：100.25 USD"))
    #expect(model.currentOrder?.state == .proposed)
    #expect(await http.actions == ["preview"])

    await model.performReviewedAction(review.id)
    #expect(model.currentOrder?.state == .accepted)
    #expect(await http.actions == ["preview", "confirm", "submit"])
    await model.performReviewedAction(review.id)
    #expect(await http.actions == ["preview", "confirm", "submit"])
}

@Test @MainActor func paperStepsRequireSeparateExplicitActionsAndConsumeReviewOnce() async throws {
    let http = OrdersHTTP()
    let model = try ordersModel(http)
    await model.refresh()
    await model.createDraft(symbol: "AAPL", side: .buy, quantity: "2", limitPrice: "100.1250")
    #expect(model.currentOrder?.state == .draft)
    #expect(await http.actions == ["orders"])
    await model.refresh()
    #expect(await http.actions == ["orders"])
    await model.previewCurrentOrder()
    #expect(model.currentOrder?.state == .proposed)
    #expect(await http.actions == ["orders", "preview"])
    model.reviewConfirmation()
    let confirm = try #require(model.review)
    #expect(confirm.summary.contains("100.25") && confirm.summary.contains("1.00"))
    #expect(await http.actions == ["orders", "preview"])
    await model.performReviewedAction(UUID())
    #expect(await http.actions == ["orders", "preview"])
    await model.performReviewedAction(confirm.id)
    await model.performReviewedAction(confirm.id)
    #expect(model.currentOrder?.state == .confirmed)
    #expect(await http.actions == ["orders", "preview", "confirm"])
    await model.refresh()
    #expect(model.canSubmit(try #require(model.currentOrder), now: ordersNow))
    model.reviewSubmission()
    let submit = try #require(model.review)
    #expect(submit.id != confirm.id)
    #expect(await http.actions.count == 3)
    await model.performReviewedAction(submit.id)
    await model.performReviewedAction(submit.id)
    #expect(model.currentOrder?.state == .accepted)
    #expect(await http.actions == ["orders", "preview", "confirm", "submit"])
    let posts = await http.posts
    let bodies = try posts.map { request in
        let data = try #require(request.httpBody)
        return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }
    #expect(Set(bodies.compactMap { $0["client_request_id"] as? String }).count == 4)
    #expect(bodies[2]["expires_at"] as? String == ordersExpiry)
    #expect(bodies[2]["user_action"] as? String == "confirm_paper_order" && bodies[3]["user_action"] as? String == "submit_paper_order")
}

@Test @MainActor func paperRestoredConfirmationNeverArmsSubmission() async throws {
    let http = OrdersHTTP(state: "confirmed")
    let model = try ordersModel(http)
    await model.refresh(); await selectOrder(model)
    #expect(!model.canSubmit(try #require(model.currentOrder), now: ordersNow))
    model.reviewSubmission()
    #expect(model.review == nil)
    await model.refresh()
    #expect(await http.posts.isEmpty)
}

@Test @MainActor func paperTimeoutBecomesReadOnlyUncertaintyWithoutResend() async throws {
    let http = OrdersHTTP(state: "proposed")
    let model = try ordersModel(http)
    await model.refresh(); await selectOrder(model)
    model.reviewConfirmation()
    await model.performReviewedAction(try #require(model.review).id)
    await http.failAfter("submit")
    model.reviewSubmission()
    await model.performReviewedAction(try #require(model.review).id)
    #expect(model.uncertainty != nil && model.actionError != nil)
    await model.refresh(); await model.refresh()
    #expect(model.currentOrder?.state == .unknown && model.uncertainty != nil)
    model.reviewSubmission(); model.reviewCancellation(); model.reviewConfirmation()
    await model.previewCurrentOrder()
    model.newTicket()
    await model.createDraft(symbol: "AAPL", side: .buy, quantity: "2", limitPrice: "100.1250")
    #expect(await http.actions == ["confirm", "submit"])
    let restored = try ordersModel(http)
    await restored.refresh(); await selectOrder(restored)
    #expect(restored.currentOrder?.state == .unknown)
    #expect(await http.actions.count == 2)
}

@Test @MainActor func paperSingleCancellationRequiresReviewAndKeepsPendingVisible() async throws {
    let http = OrdersHTTP(state: "partial")
    let model = try ordersModel(http)
    await model.refresh(); await selectOrder(model)
    model.reviewCancellation()
    let review = try #require(model.review)
    #expect(review.kind == "cancel" && review.order.id == ordersID)
    #expect(await http.posts.isEmpty)
    model.discardReview()
    await model.performReviewedAction(review.id)
    #expect(await http.posts.isEmpty)
    model.reviewCancellation()
    let active = try #require(model.review)
    await model.performReviewedAction(active.id)
    await model.performReviewedAction(active.id)
    #expect(model.currentOrder?.state == .cancelPending && model.currentOrder?.state.label == "撤单处理中")
    model.reviewCancellation()
    #expect(model.review == nil)
    #expect(await http.actions == ["cancel"])
    await http.setState("filled", filled: 2)
    await model.refresh()
    #expect(model.currentOrder?.state == .filled && model.currentOrder?.filledQuantity == 2)
    #expect(await http.actions == ["cancel"])
    await http.setState("cancelled", filled: 2)
    await model.refresh()
    #expect(model.currentOrder?.state == .filled)
}

@Test @MainActor func paperStaleVersionAndChangedSessionPreventWrites() async throws {
    let http = OrdersHTTP(state: "proposed")
    let model = try ordersModel(http)
    await model.refresh(); await selectOrder(model)
    model.reviewConfirmation()
    let review = try #require(model.review)
    await http.changeVersion()
    await model.performReviewedAction(review.id)
    #expect(model.actionError != nil)
    #expect(await http.posts.isEmpty)
    let other = OrdersHTTP(state: "draft")
    let otherModel = try ordersModel(other)
    await otherModel.refresh(); await selectOrder(otherModel)
    await other.changeProtectedSession()
    await otherModel.previewCurrentOrder()
    #expect(!otherModel.executionReady && otherModel.actionError != nil)
    #expect(await other.posts.isEmpty)
}

@Test @MainActor func paperMissingTargetLiveAndMissingExecutionGateFailClosed() async throws {
    for kind in ["target", "live", "gate"] {
        let http = OrdersHTTP(state: "draft")
        switch kind {
        case "target": await http.removeTarget()
        case "live": await http.setLive()
        default: await http.removeGate("permissions_verified")
        }
        let model = try ordersModel(http)
        await model.refresh(); await selectOrder(model)
        #expect(!model.executionReady)
        if kind != "gate" {
            #expect(!model.canCreateDraft)
            await model.createDraft(symbol: "AAPL", side: .buy, quantity: "2", limitPrice: "100.1250")
        }
        await model.previewCurrentOrder()
        #expect(await http.posts.isEmpty)
    }
}

@Test @MainActor func paperExpiredAndIncompletePreviewsCannotBeConfirmed() async throws {
    let http = OrdersHTTP(state: "proposed")
    let model = try ordersModel(http, now: Date(timeIntervalSince1970: 2_000_000_000))
    await model.refresh(); await selectOrder(model)
    model.reviewConfirmation()
    #expect(model.review == nil)
    #expect(await http.posts.isEmpty)
    let incomplete = OrdersHTTP(state: "proposed")
    await incomplete.incompletePreview()
    let other = try ordersModel(incomplete)
    await other.refresh(); await selectOrder(other)
    other.reviewConfirmation()
    #expect(other.review == nil)
    #expect(await incomplete.posts.isEmpty)
}

@Test @MainActor func paperInvalidResponseAfterPostIsUncertainAndDoesNotRetry() async throws {
    let http = OrdersHTTP(state: "draft")
    let model = try ordersModel(http)
    await model.refresh(); await selectOrder(model)
    await http.corruptResponse("preview")
    await model.previewCurrentOrder()
    #expect(model.uncertainty != nil)
    await model.previewCurrentOrder()
    #expect(await http.actions == ["preview"])
}

@Test @MainActor func paperInvalidTicketNeverPosts() async throws {
    let http = OrdersHTTP()
    let model = try ordersModel(http)
    await model.refresh()
    for quantity in ["0", "1.5", "-1", "100001"] {
        await model.createDraft(symbol: "AAPL", side: .buy, quantity: quantity, limitPrice: "100.1250")
        #expect(model.actionError != nil)
    }
    #expect(await http.posts.isEmpty)
}
#endif
