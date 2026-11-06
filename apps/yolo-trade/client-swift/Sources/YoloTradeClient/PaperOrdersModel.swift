import Foundation
import SwiftUI
import YoloAgentsSDK
import YoloAgentsUI

@MainActor
public final class PaperOrdersModel: ObservableObject {
    @Published public private(set) var orders: [PaperOrder] = []
    @Published public var selectedID: UUID?
    @Published public private(set) var detail: PaperOrderDetail?
    @Published public private(set) var health: PaperHealth?
    @Published public private(set) var broker: PaperBrokerHealth?
    @Published public private(set) var isRefreshing = false
    @Published public private(set) var isWriting = false
    @Published public private(set) var readError: String?
    @Published public private(set) var actionError: String?
    @Published public private(set) var uncertainty: String?
    private let connection: PairedConnection
    private let client: PaperAPIClient
    private let clock: @Sendable () -> Date
    private var fetchedAt: Date?
    private var authorizationFailed = false
    private let transport: any HTTPTransport
    private let platform: DevicePlatform
    @Published public private(set) var deviceAuthorization: TradeDeviceAuthorization?
    @Published public private(set) var deviceAuthorizationError: String?
    @Published public private(set) var tradingDevices: [TradePairedPhone] = []
    public var isDeviceManager: Bool {
        platform == .macos && connection.pairing.device.platform == .macos && connection.pairing.device.revokedAt == nil &&
            connection.pairing.device.permissions.contains("devices:manage") && !authorizationFailed
    }
    private var writer: PaperOrderWriter? {
        guard platform == connection.pairing.device.platform else { return nil }
        return try? PaperOrderWriter(connection: connection, authorization: deviceAuthorization, transport: transport)
    }
    private var uncertainRequest: (operation: String, requestID: UUID, orderID: UUID?)?
    private var armedConfirmation: PaperOrderConfirmation?
    @Published public private(set) var review: PaperOrderReview?

    public init(connection: PairedConnection, platform: DevicePlatform = AccountAccess.currentPlatform,
                transport: any HTTPTransport = URLSessionTransport(), clock: @escaping @Sendable () -> Date = { Date() }) {
        self.connection = connection; self.clock = clock; self.transport = transport; self.platform = platform
        client = PaperAPIClient(endpoint: connection.endpoint, token: connection.pairing.token, transport: transport)
    }
    public var canManage: Bool {
        writer != nil && !authorizationFailed && deviceAuthorization?.permitsPaper == true && fetchedAt.map { clock().timeIntervalSince($0) < 15 } == true
    }
    public var accountSelection: PaperAccountSelection? { broker.flatMap { try? PaperAccountSelection(broker: $0) } }
    public var executionReady: Bool {
        guard canManage, let health, let broker, let fetchedAt, clock().timeIntervalSince(fetchedAt) < 15,
              health.paperExecutionReady, health.broker?.configuration?.configID == broker.configuration?.configID,
              health.broker?.sessionID == broker.sessionID else { return false }
        return broker.configuration?.environment == "paper" && broker.configuration?.readOnly == false &&
            broker.configuration?.executionEnabled == true && broker.connected == true && broker.executionEnabled == true &&
            broker.environmentVerified == true && broker.accountVerified == true && broker.permissionsVerified == true &&
            broker.dataComplete == true && broker.healthStale == false && broker.sessionID?.isEmpty == false
    }
    public var currentOrder: PaperOrder? { orders.first { $0.id == selectedID } }

    /// Reads only. Lifecycle refresh, view reconstruction and selection never invoke a writer.
    public func refresh() async {
        guard !isRefreshing, !isWriting else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            try await fetchHealth()
            let values = try await client.paperOrders()
            for order in values { apply(order) }
            if let pending = uncertainRequest, pending.operation == "create",
               let recovered = values.first(where: { $0.clientRequestID == pending.requestID }) {
                selectedID = recovered.id; uncertainRequest = nil; uncertainty = nil
            }
            if let id = selectedID { try await readDetail(id) }
            readError = nil
        } catch { recordReadError(error) }
    }
    public func loadSelected() async {
        guard let id = selectedID else { detail = nil; return }
        do { try await readDetail(id); readError = nil } catch { recordReadError(error) }
    }
    private func fetchHealth() async throws {
        fetchedAt = nil
        guard connection.agentID == nil || connection.agentID == YoloTradeProduct.agentID else { throw ClientError.agentIdentityMismatch }
        let publicHealth = try await client.health()
        guard publicHealth.serverID == connection.pairing.serverID, publicHealth.apiVersion == 1 else { throw ClientError.incompatibleServer }
        guard publicHealth.agentID == nil || publicHealth.agentID == YoloTradeProduct.agentID else { throw ClientError.agentIdentityMismatch }
        let protectedBroker = try await client.paperBrokerHealth()
        do { try await refreshDeviceAuthorization(includeDevices: false) }
        catch { deviceAuthorization = nil; review = nil; armedConfirmation = nil }
        health = publicHealth; broker = protectedBroker; fetchedAt = clock()
    }
    public func refreshDeviceAuthorization(includeDevices: Bool = true) async throws {
        do {
            guard connection.agentID == nil || connection.agentID == YoloTradeProduct.agentID else { throw ClientError.agentIdentityMismatch }
            let publicHealth = try await client.health()
            guard publicHealth.serverID == connection.pairing.serverID, publicHealth.apiVersion == 1 else { throw ClientError.incompatibleServer }
            guard publicHealth.agentID == nil || publicHealth.agentID == YoloTradeProduct.agentID else { throw ClientError.agentIdentityMismatch }
            let response = try await client.tradeAuthorization()
            guard response.deviceID == connection.pairing.device.id else { throw PaperOrderValidationError.permissionDenied }
            if deviceAuthorization != response { review = nil; armedConfirmation = nil }
            deviceAuthorization = response
            if includeDevices && isDeviceManager { tradingDevices = try await client.tradeDevices() }
            deviceAuthorizationError = nil
        } catch {
            deviceAuthorization = nil; tradingDevices = []; review = nil; armedConfirmation = nil
            deviceAuthorizationError = error.localizedDescription
            throw error
        }
    }
    public func setDeviceAuthorization(_ deviceID: UUID, authorized: Bool) async {
        guard isDeviceManager, !isWriting, !isRefreshing else { return }
        guard tradingDevices.contains(where: { $0.deviceID == deviceID && $0.revokedAt == nil }) else {
            actionError = PaperOrderValidationError.permissionDenied.localizedDescription; return
        }
        isWriting = true
        defer { isWriting = false }
        do {
            try await fetchHealth()
            let response = try await client.setTradeAuthorization(deviceID, authorized: authorized)
            guard response.deviceID == deviceID, response.paperTradingEnabled == authorized else { throw PaperOrderValidationError.invalidState }
            tradingDevices = try await client.tradeDevices(); actionError = nil; deviceAuthorizationError = nil
        } catch { actionError = error.localizedDescription }
    }
    private func readDetail(_ id: UUID) async throws {
        let value = try await client.paperOrder(id)
        guard value.order.id == id else { throw PaperOrderValidationError.invalidState }
        apply(value.order)
        if selectedID == id, currentOrder == value.order { detail = value }
        if let pending = uncertainRequest, pending.orderID == id {
            let finalReply = [.accepted, .working, .partial, .filled, .rejected, .cancelled].contains(value.order.state)
            let previewResolved = pending.operation == "preview" && [.draft, .proposed].contains(value.order.state)
            if finalReply || previewResolved { uncertainRequest = nil; uncertainty = nil }
        }
    }
    private func apply(_ order: PaperOrder) {
        if let index = orders.firstIndex(where: { $0.id == order.id }) {
            let old = orders[index]
            guard order.version >= old.version, order.updatedAt >= old.updatedAt, order.filledQuantity >= old.filledQuantity else { return }
            guard old.state != .filled || order.state == .filled else { return }
            orders[index] = order
        } else { orders.append(order) }
        orders.sort { $0.createdAt > $1.createdAt }
        if let armed = armedConfirmation, currentOrder?.confirmation != armed { armedConfirmation = nil }
    }
    private func recordReadError(_ error: Error) {
        guard !(error is CancellationError), !Task.isCancelled else { return }
        readError = error.localizedDescription
        if case ClientError.http(let status, _, _) = error, status == 401 || status == 403 {
            authorizationFailed = true; deviceAuthorization = nil; review = nil; armedConfirmation = nil
        }
    }

    public var canCreateDraft: Bool { canManage && accountSelection != nil && !isWriting && !isRefreshing && uncertainRequest == nil }
    public func newTicket() { selectedID = nil; detail = nil; review = nil; armedConfirmation = nil; actionError = nil }
    public func canPreview(_ order: PaperOrder) -> Bool {
        executionReady && !isWriting && uncertainRequest == nil && order.hasSupportedShape &&
            [.draft, .proposed].contains(order.state) && order.confirmation == nil
    }
    public func canConfirm(_ order: PaperOrder, now: Date) -> Bool {
        executionReady && !isWriting && uncertainRequest == nil && order.state == .proposed &&
            order.confirmation == nil && order.hasValidPreview(at: now)
    }
    public func canSubmit(_ order: PaperOrder, now: Date) -> Bool {
        executionReady && !isWriting && uncertainRequest == nil && order.state == .confirmed &&
            order.hasValidPreview(at: now) && order.confirmation != nil && order.confirmation == armedConfirmation
    }
    public func canCancel(_ order: PaperOrder) -> Bool {
        executionReady && !isWriting && uncertainRequest == nil && order.hasSupportedShape &&
            order.state.canCancel && order.filledQuantity < order.quantity
    }
    public func createDraft(symbol: String, side: PaperOrderSide, quantity: String, limitPrice: String) async {
        guard platform == .macos, let contract = try? PaperStockContract(symbol: symbol) else { return }
        await createDraft(contract: contract, side: side, quantity: quantity, limitPrice: limitPrice)
    }
    public func createDraft(contract: PaperStockContract, side: PaperOrderSide, quantity: String, limitPrice: String) async {
        guard canCreateDraft, let writer, let selection = accountSelection else { return }
        guard quantity.range(of: #"^[1-9][0-9]{0,5}$"#, options: .regularExpression) != nil, let count = Int(quantity) else {
            actionError = PaperOrderValidationError.invalidInput.localizedDescription; return
        }
        var sent = false
        let requestID = UUID()
        isWriting = true
        defer { isWriting = false }
        do {
            let request: PaperOrderDraftRequest
            if contract.conID != nil {
                request = try PaperOrderDraftRequest(clientRequestID: requestID, accountSelection: selection,
                    contract: contract, side: side, quantity: count, limitPrice: limitPrice)
            } else {
                guard platform == .macos else { throw PaperOrderValidationError.invalidInput }
                request = try PaperOrderDraftRequest(clientRequestID: requestID, accountSelection: selection,
                    symbol: contract.symbol, side: side, quantity: count, limitPrice: limitPrice)
            }
            try await fetchHealth()
            guard canManage, selection == accountSelection else { throw PaperOrderValidationError.targetUnavailable }
            sent = true
            let order = try await writer.createDraft(request)
            guard order.clientRequestID == requestID, order.accountSelection == selection, order.contract == contract,
                  order.hasSupportedShape, order.side == side, order.quantity == count,
                  Decimal(string: order.limitPrice) == Decimal(string: limitPrice) else { throw PaperOrderValidationError.invalidState }
            selectedID = order.id; apply(order); detail = nil; armedConfirmation = nil; review = nil; actionError = nil
        } catch { writeFailed(error, operation: "create", requestID: requestID, orderID: nil, sent: sent) }
    }
    public func previewCurrentOrder() async {
        guard let order = currentOrder, canPreview(order), let writer else { return }
        let requestID = UUID()
        var sent = false
        isWriting = true
        defer { isWriting = false }
        do {
            let request = try PaperOrderPreviewRequest(clientRequestID: requestID, order: order)
            try await preflight(order)
            sent = true
            let updated = try await writer.preview(order.id, request: request)
            guard sameContent(updated, order, allowsPreview: true), updated.state == .proposed, updated.hasValidPreview(at: clock()) else { throw PaperOrderValidationError.invalidState }
            apply(updated); armedConfirmation = nil; review = nil; actionError = nil
        } catch { writeFailed(error, operation: "preview", requestID: requestID, orderID: order.id, sent: sent) }
    }
    /// Obtain What-if if needed, then prepare one explicit confirmation whose
    /// accepted action continues through confirm and submit without another UI step.
    public func prepareCompleteSubmission() async {
        guard !isWriting, uncertainRequest == nil else { return }
        if let order = currentOrder, order.state == .draft, canPreview(order) {
            await previewCurrentOrder()
        }
        guard let order = currentOrder, canConfirm(order, now: clock()) else { return }
        do {
            review = PaperOrderReview(order: order, authorization: deviceAuthorization, action: .confirmAndSubmit(
                try PaperOrderConfirmRequest(clientRequestID: UUID(), order: order, now: clock())
            ))
            actionError = nil
        } catch { actionError = error.localizedDescription }
    }
    public func reviewConfirmation() {
        guard let order = currentOrder, canConfirm(order, now: clock()) else { return }
        do { review = PaperOrderReview(order: order, authorization: deviceAuthorization, action: .confirm(try PaperOrderConfirmRequest(clientRequestID: UUID(), order: order, now: clock()))) }
        catch { actionError = error.localizedDescription }
    }
    public func reviewSubmission() {
        guard let order = currentOrder, canSubmit(order, now: clock()) else { return }
        do { review = PaperOrderReview(order: order, authorization: deviceAuthorization, action: .submit(try PaperOrderSubmitRequest(clientRequestID: UUID(), order: order, now: clock()))) }
        catch { actionError = error.localizedDescription }
    }
    public func reviewCancellation() {
        guard let order = currentOrder, canCancel(order) else { return }
        do { review = PaperOrderReview(order: order, authorization: deviceAuthorization, action: .cancel(try PaperOrderCancelRequest(clientRequestID: UUID(), order: order))) }
        catch { actionError = error.localizedDescription }
    }
    public func discardReview() { review = nil }
    /// Only the matching review token can invoke one writer call. There is no retry scheduler.
    public func performReviewedAction(_ reviewID: UUID) async {
        guard !isWriting, canManage, let writer, let pending = review, pending.id == reviewID,
              currentOrder == pending.order, uncertainRequest == nil else { return }
        review = nil
        isWriting = true
        defer { isWriting = false }
        var sent = false
        do {
            try await preflight(pending.order)
            guard pending.authorization == deviceAuthorization else { throw PaperOrderValidationError.permissionDenied }
            let updated: PaperOrder
            switch pending.action {
            case .confirm(let request):
                _ = try PaperOrderConfirmRequest(clientRequestID: request.clientRequestID, order: pending.order, now: clock())
                sent = true; updated = try await writer.confirm(pending.order.id, request: request)
            case .submit(let request):
                guard pending.order.confirmation == armedConfirmation else { throw PaperOrderValidationError.invalidState }
                _ = try PaperOrderSubmitRequest(clientRequestID: request.clientRequestID, order: pending.order, now: clock())
                armedConfirmation = nil
                sent = true; updated = try await writer.submit(pending.order.id, request: request)
            case .confirmAndSubmit(let request):
                _ = try PaperOrderConfirmRequest(clientRequestID: request.clientRequestID,
                                                 order: pending.order, now: clock())
                sent = true
                let confirmed = try await writer.confirm(pending.order.id, request: request)
                guard sameContent(confirmed, pending.order), confirmed.state == .confirmed,
                      confirmed.contract == pending.order.contract, confirmed.side == pending.order.side,
                      confirmed.quantity == pending.order.quantity, confirmed.limitPrice == pending.order.limitPrice,
                      confirmed.accountSelection == pending.order.accountSelection,
                      confirmed.contentHash == pending.order.contentHash, confirmed.preview == pending.order.preview,
                      let confirmation = confirmed.confirmation,
                      confirmation.version == confirmed.version,
                      confirmation.contentHash == confirmed.contentHash,
                      confirmed.hasValidPreview(at: clock()) else {
                    throw PaperOrderValidationError.invalidState
                }
                apply(confirmed)
                armedConfirmation = confirmation
                try await preflight(confirmed)
                guard pending.authorization == deviceAuthorization, currentOrder?.confirmation == armedConfirmation else {
                    throw PaperOrderValidationError.invalidState
                }
                let submit = try PaperOrderSubmitRequest(clientRequestID: UUID(), order: confirmed, now: clock())
                armedConfirmation = nil
                updated = try await writer.submit(confirmed.id, request: submit)
            case .cancel(let request):
                sent = true; updated = try await writer.cancel(pending.order.id, request: request)
            }
            guard sameContent(updated, pending.order) else { throw PaperOrderValidationError.invalidState }
            apply(updated)
            if case .confirm = pending.action, updated.preview != pending.order.preview {
                throw PaperOrderValidationError.invalidState
            }
            if case .confirm = pending.action, updated.state == .confirmed, let confirmation = updated.confirmation,
               confirmation.version == updated.version, confirmation.contentHash == updated.contentHash,
               updated.hasValidPreview(at: clock()) { armedConfirmation = confirmation }
            actionError = nil
        } catch {
            writeFailed(error, operation: pending.kind, requestID: pending.requestID, orderID: pending.order.id, sent: sent)
        }
    }
    private func sameContent(_ updated: PaperOrder, _ original: PaperOrder, allowsPreview: Bool = false) -> Bool {
        updated.hasSupportedShape && updated.id == original.id && updated.clientRequestID == original.clientRequestID &&
            updated.accountSelection == original.accountSelection && updated.environment == original.environment &&
            updated.contract == original.contract && updated.side == original.side && updated.quantity == original.quantity &&
            updated.limitPrice == original.limitPrice && updated.orderType == original.orderType && updated.tif == original.tif &&
            (updated.contentHash == original.contentHash || (allowsPreview && updated.version > original.version)) && updated.version >= original.version
    }
    private func preflight(_ order: PaperOrder) async throws {
        try await fetchHealth()
        guard executionReady, accountSelection == order.accountSelection else { throw PaperOrderValidationError.executionUnavailable }
        let fresh = try await client.paperOrder(order.id)
        apply(fresh.order)
        guard fresh.order == order, selectedID == order.id else { throw PaperOrderValidationError.invalidState }
    }
    private func writeFailed(_ error: Error, operation: String, requestID: UUID, orderID: UUID?, sent: Bool) {
        actionError = error.localizedDescription
        var ambiguous = true
        if case ClientError.http(let status, let code, _) = error { ambiguous = status >= 500 || status == 408 || code == "broker_outcome_unknown" }
        if sent && ambiguous {
            uncertainRequest = (operation, requestID, orderID)
            uncertainty = "操作结果尚未确认。只刷新订单核对状态，不会自动重发或继续提交。"
            armedConfirmation = nil
        }
        if case ClientError.http(let status, _, _) = error, status == 401 || status == 403 {
            authorizationFailed = true; deviceAuthorization = nil; review = nil; armedConfirmation = nil
        }
    }
}

public struct PaperOrderReview: Identifiable, Sendable {
    public let id = UUID()
    public let order: PaperOrder
    fileprivate let authorization: TradeDeviceAuthorization?
    fileprivate let action: Action
    fileprivate enum Action: Sendable {
        case confirm(PaperOrderConfirmRequest), submit(PaperOrderSubmitRequest)
        case confirmAndSubmit(PaperOrderConfirmRequest), cancel(PaperOrderCancelRequest)
    }
    fileprivate init(order: PaperOrder, authorization: TradeDeviceAuthorization?, action: Action) { self.order = order; self.authorization = authorization; self.action = action }
    public var kind: String {
        switch action { case .confirm: "confirm"; case .submit: "submit"; case .confirmAndSubmit: "confirm-and-submit"; case .cancel: "cancel" }
    }
    public var title: String {
        switch action { case .confirm: "确认此 Paper 预览？"; case .submit: "提交此 Paper 订单？"; case .confirmAndSubmit: "确认并提交此 Paper 订单？"; case .cancel: "撤销此 Paper 订单？" }
    }
    public var buttonTitle: String {
        switch action { case .confirm: "确认预览（不提交）"; case .submit: "提交 Paper 订单"; case .confirmAndSubmit: "确认并提交 Paper 订单"; case .cancel: "确认撤销此单" }
    }
    fileprivate var requestID: UUID {
        switch action { case .confirm(let request): request.clientRequestID; case .submit(let request): request.clientRequestID; case .confirmAndSubmit(let request): request.clientRequestID; case .cancel(let request): request.clientRequestID }
    }
    public var summary: String {
        var text = "\(order.side.rawValue) \(order.contract.symbol) · \(order.quantity) 股 · 限价 \(order.limitPrice) USD\nSTK / SMART / USD · DAY / LMT"
        if let preview = order.preview {
            text += "\nconId：\(preview.qualifiedContract.conID) · 主交易所：\(preview.qualifiedContract.primaryExchange ?? "未提供")\n预览有效至：\(preview.expiresAt.date.formatted())\n初始保证金变动：\(preview.whatIf.initialMarginChange) USD\n维持保证金变动：\(preview.whatIf.maintenanceMarginChange) USD\n佣金估算：\(preview.whatIf.commission) USD"
        }
        return text
    }
}
