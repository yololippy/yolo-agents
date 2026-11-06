import Foundation
import Testing
import YoloAgentsSDK
import YoloAgentsUI
@testable import YoloTradeClient

private let ordersServerID = "40000000-0000-4000-8000-000000000004"
private let ordersDeviceID = UUID(uuidString: "50000000-0000-4000-8000-000000000005")!
private let ordersID = UUID(uuidString: "10000000-0000-4000-8000-000000000001")!
private let ordersConfig = String(repeating: "a", count: 64)
private let ordersHash = String(repeating: "b", count: 64)
private let ordersNow = Date(timeIntervalSince1970: 1_800_000_000)
private let ordersExpiry = "2030-01-01T00:00:00.123456Z"

private func ordersConnection(platform: DevicePlatform = .macos, privileged: Bool = true) throws -> PairedConnection {
    let body: [String: Any] = [
        "device": ["id": ordersDeviceID.uuidString, "name": "Fixture", "platform": platform.rawValue,
                   "permissions": privileged && platform == .macos ? ["devices:manage", "chat:read"] : ["chat:read"],
                   "created_at": "2026-10-28T00:00:00Z", "revoked_at": NSNull()],
        "token": platform == .ios ? "fixture-paper-token-ios" : "fixture-paper-token", "server_id": ordersServerID, "api_version": 1,
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
    private var corruptField = "id"
    private var protectedSession: String?
    private var healthAgent: String?
    private var phoneAuthorized = false
    private var phoneRevision: String?
    private var rotateRevisionAfterConfirm = false
    private var grantDeviceID = ordersDeviceID
    func authorizePhone(_ authorized: Bool = true) { phoneAuthorized = authorized; phoneRevision = authorized ? UUID().uuidString : nil }
    func rotatePhoneRevisionAfterConfirmation() { rotateRevisionAfterConfirm = true }
    func wrongGrantDevice() { grantDeviceID = UUID() }
    func changeBinding(_ field: String) {
        switch field {
        case "content_hash": order?[field] = String(repeating: "c", count: 64)
        case "version": order?[field] = 9
        default:
            var preview = order?["preview"] as? [String: Any] ?? [:]
            preview[field] = field == "id" ? UUID().uuidString : "2029-01-01T00:00:00Z"
            order?["preview"] = preview
        }
        tick()
    }
    private(set) var requests: [URLRequest] = []
    init(state: String? = nil) { order = state.map { ordersFixture(state: $0) } }
    func failAfter(_ action: String) { timeoutAction = action }
    func corruptResponse(_ action: String, field: String = "id") { corruptAction = action; corruptField = field }
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
            if path == "/v1/paper-trading/authorization" {
                let phone = request.value(forHTTPHeaderField: "Authorization")?.hasSuffix("-ios") == true
                return try response(["authorization": ["device_id": grantDeviceID.uuidString,
                    "paper_trading_enabled": phone ? phoneAuthorized : true,
                    "authorization_revision": phone ? (phoneRevision as Any? ?? NSNull()) : NSNull()]])
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
            order?["client_request_id"] = body["client_request_id"]; order?["contract"] = body["contract"]
            order?["side"] = body["side"]; order?["quantity"] = body["quantity"]
            if let limit = body["limit_price"] as? String { order?["limit_price"] = NSDecimalNumber(string: limit).stringValue }
        case "preview":
            let original = order; let contract = order?["contract"]
            order = ordersFixture(state: "proposed")
            for key in ["client_request_id", "contract", "quantity", "side", "limit_price"] { order?[key] = original?[key] }
            order?["content_hash"] = String(repeating: "d", count: 64)
            if let exact = contract as? [String: Any], exact["con_id"] != nil {
                var preview = order?["preview"] as? [String: Any] ?? [:]
                preview["qualified_contract"] = exact; order?["preview"] = preview
            }
        case "confirm":
            let original = order
            order = ordersFixture(state: "confirmed")
            for key in ["client_request_id", "contract", "quantity", "side", "limit_price", "preview", "content_hash"] { order?[key] = original?[key] }
            var confirmation = order?["confirmation"] as? [String: Any] ?? [:]
            confirmation["content_hash"] = original?["content_hash"]; order?["confirmation"] = confirmation
            if rotateRevisionAfterConfirm { phoneRevision = UUID().uuidString }
        case "submit": order?["state"] = timeoutAction == action ? "unknown" : "accepted"
        case "cancel": order?["state"] = "cancel_pending"
        default: throw URLError(.badURL)
        }
        tick()
        if timeoutAction == action { throw URLError(.timedOut) }
        var reply = try #require(order)
        if corruptAction == action {
            if corruptField == "content_hash" { reply[corruptField] = String(repeating: "c", count: 64) }
            else if corruptField == "preview" {
                var preview = reply["preview"] as? [String: Any] ?? [:]; preview["id"] = UUID().uuidString; reply["preview"] = preview
            } else { reply["id"] = UUID().uuidString }
        }
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
    await http.changeHealthAgent("yolo-beni")
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

@Test @MainActor func paperOrdersPageKeepsItsOwnNavigationAndModalCallersKeepDismissal() throws {
    let connection = try ordersConnection(platform: .ios, privileged: false)
    let sheet = PaperOrdersView(connection: connection)
    #expect(sheet.presentation == .modal)
    #expect(sheet.presentation.showsDismissAction)
    let page = PaperOrdersView(connection: connection, presentation: .page)
    #expect(!page.presentation.showsDismissAction)
    let feature = YoloTradeRootView.paperOrdersPage(connection: connection)
    #if os(iOS)
    #expect(feature.presentation == .page)
    #else
    #expect(feature.presentation == .modal)
    #endif
}

@Test @MainActor func authorizedPhoneUsesExistingStateMachineAndRequiresExplicitReview() async throws {
    let http = OrdersHTTP()
    await http.authorizePhone()
    let model = try ordersModel(http, platform: .ios)
    await model.refresh()
    #expect(model.canManage && model.executionReady && !model.isDeviceManager)
    let contract = try PaperStockContract(symbol: "AAPL", conID: 265598, primaryExchange: "NASDAQ")
    await model.createDraft(contract: contract, side: .buy, quantity: "2", limitPrice: "")
    #expect(await http.posts.isEmpty)
    await model.createDraft(contract: contract, side: .buy, quantity: "2", limitPrice: "100.1250")
    await model.previewCurrentOrder()
    #expect(model.currentOrder?.contract == contract)
    #expect(await http.actions == ["orders", "preview"])
    await model.prepareCompleteSubmission()
    let review = try #require(model.review)
    #expect(review.summary.contains("NASDAQ"))
    #expect(await http.actions == ["orders", "preview"])
    await model.performReviewedAction(review.id)
    #expect(model.currentOrder?.state == .accepted)
    #expect(await http.actions == ["orders", "preview", "confirm", "submit"])
}

@Test @MainActor func phoneGrantChangesAndWrongDevicePreventConfirmation() async throws {
    for kind in ["revoked", "revision", "device"] {
        let http = OrdersHTTP(state: "proposed")
        await http.authorizePhone()
        let model = try ordersModel(http, platform: .ios)
        await model.refresh(); await selectOrder(model)
        model.reviewConfirmation()
        let review = try #require(model.review)
        switch kind {
        case "revoked": await http.authorizePhone(false)
        case "revision": await http.authorizePhone(false); await http.authorizePhone()
        default: await http.wrongGrantDevice()
        }
        await model.performReviewedAction(review.id)
        #expect(model.actionError != nil)
        #expect(await http.posts.isEmpty)
    }
}

@Test @MainActor func everyPreviewBindingChangeStopsReviewedConfirmation() async throws {
    for field in ["version", "content_hash", "id", "expires_at"] {
        let http = OrdersHTTP(state: "proposed")
        let model = try ordersModel(http)
        await model.refresh(); await selectOrder(model)
        model.reviewConfirmation()
        let review = try #require(model.review)
        await http.changeBinding(field)
        await model.performReviewedAction(review.id)
        #expect(model.actionError != nil)
        #expect(await http.posts.isEmpty)
    }
}

@Test @MainActor func phoneUnknownOutcomeNeverResendsAfterRefreshOrReopen() async throws {
    let http = OrdersHTTP(state: "proposed")
    await http.authorizePhone()
    let model = try ordersModel(http, platform: .ios)
    await model.refresh(); await selectOrder(model)
    await model.prepareCompleteSubmission()
    let review = try #require(model.review)
    await http.failAfter("submit")
    await model.performReviewedAction(review.id)
    await model.refresh(); await model.refresh()
    await model.prepareCompleteSubmission()
    await model.performReviewedAction(review.id)
    #expect(model.currentOrder?.state == .unknown && model.uncertainty != nil)
    #expect(await http.actions == ["confirm", "submit"])
    let reopened = try ordersModel(http, platform: .ios)
    await reopened.refresh(); await selectOrder(reopened)
    #expect(!reopened.canSubmit(try #require(reopened.currentOrder), now: ordersNow))
    #expect(await http.actions == ["confirm", "submit"])
}

@Test @MainActor func changedConfirmationResponseNeverContinuesToSubmission() async throws {
    for field in ["content_hash", "preview"] {
        let http = OrdersHTTP(state: "proposed")
        let model = try ordersModel(http)
        await model.refresh(); await selectOrder(model)
        await model.prepareCompleteSubmission()
        let review = try #require(model.review)
        await http.corruptResponse("confirm", field: field)
        await model.performReviewedAction(review.id)
        #expect(model.uncertainty != nil && model.actionError != nil)
        #expect(await http.actions == ["confirm"])
        await model.refresh()
        #expect(!model.canSubmit(try #require(model.currentOrder), now: ordersNow))
        #expect(await http.actions == ["confirm"])
    }
}

@Test @MainActor func realServerPriceNormalizationAndNewPreviewHashRemainUsable() async throws {
    let http = OrdersHTTP()
    let model = try ordersModel(http)
    await model.refresh()
    await model.createDraft(symbol: "AAPL", side: .buy, quantity: "2", limitPrice: "100.1250")
    #expect(model.currentOrder?.limitPrice == "100.125" && model.uncertainty == nil)
    let oldHash = model.currentOrder?.contentHash
    await model.previewCurrentOrder()
    let order = try #require(model.currentOrder)
    #expect(order.contentHash != oldHash && model.canConfirm(order, now: ordersNow))
    let actions = await http.actions
    #expect(model.uncertainty == nil && actions == ["orders", "preview"])
}

@Test @MainActor func phoneRevisionChangePreventsReviewedSubmission() async throws {
    let http = OrdersHTTP(state: "proposed")
    await http.authorizePhone()
    let model = try ordersModel(http, platform: .ios)
    await model.refresh(); await selectOrder(model)
    model.reviewConfirmation()
    await model.performReviewedAction(try #require(model.review).id)
    model.reviewSubmission()
    let review = try #require(model.review)
    await http.authorizePhone(false); await http.authorizePhone()
    await model.performReviewedAction(review.id)
    #expect(model.actionError != nil)
    #expect(await http.actions == ["confirm"])
    #expect(!model.canSubmit(try #require(model.currentOrder), now: ordersNow))
}

@Test @MainActor func phoneRevisionChangeBetweenConfirmAndSubmitStopsContinuation() async throws {
    let http = OrdersHTTP(state: "proposed")
    await http.authorizePhone()
    let model = try ordersModel(http, platform: .ios)
    await model.refresh(); await selectOrder(model)
    await model.prepareCompleteSubmission()
    let review = try #require(model.review)
    await http.rotatePhoneRevisionAfterConfirmation()
    await model.performReviewedAction(review.id)
    #expect(model.actionError != nil && model.uncertainty != nil)
    #expect(await http.actions == ["confirm"])
    await model.refresh()
    await model.performReviewedAction(review.id)
    #expect(await http.actions == ["confirm"])
}
