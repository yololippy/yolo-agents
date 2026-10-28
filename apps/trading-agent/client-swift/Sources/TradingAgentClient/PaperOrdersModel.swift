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
    #if os(macOS)
    private let writer: PaperOrderWriter?
    private var uncertainRequest: (operation: String, requestID: UUID, orderID: UUID?)?
    private var armedConfirmation: PaperOrderConfirmation?
    @Published public private(set) var review: PaperOrderReview?
    #endif

    public init(connection: PairedConnection, platform: DevicePlatform = AccountAccess.currentPlatform,
                transport: any HTTPTransport = URLSessionTransport(), clock: @escaping @Sendable () -> Date = { Date() }) {
        self.connection = connection; self.clock = clock
        client = PaperAPIClient(endpoint: connection.endpoint, token: connection.pairing.token, transport: transport)
        #if os(macOS)
        writer = platform == .macos ? try? PaperOrderWriter(connection: connection, transport: transport) : nil
        #endif
    }
    public var canManage: Bool {
        #if os(macOS)
        writer != nil && !authorizationFailed
        #else
        false
        #endif
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
            #if os(macOS)
            if let pending = uncertainRequest, pending.operation == "create",
               let recovered = values.first(where: { $0.clientRequestID == pending.requestID }) {
                selectedID = recovered.id; uncertainRequest = nil; uncertainty = nil
            }
            #endif
            if let id = selectedID { try await readDetail(id) }
            readError = nil
        } catch { recordReadError(error) }
    }
    public func loadSelected() async {
        guard let id = selectedID else { detail = nil; return }
        do { try await readDetail(id); readError = nil } catch { recordReadError(error) }
    }
    private func fetchHealth() async throws {
        guard connection.agentID == nil || connection.agentID == "trading-agent" else { throw ClientError.agentIdentityMismatch }
        let publicHealth = try await client.health()
        guard publicHealth.serverID == connection.pairing.serverID, publicHealth.apiVersion == 1 else { throw ClientError.incompatibleServer }
        guard publicHealth.agentID == nil || publicHealth.agentID == "trading-agent" else { throw ClientError.agentIdentityMismatch }
        let protectedBroker = try await client.paperBrokerHealth()
        health = publicHealth; broker = protectedBroker; fetchedAt = clock()
    }
    private func readDetail(_ id: UUID) async throws {
        let value = try await client.paperOrder(id)
        guard value.order.id == id else { throw PaperOrderValidationError.invalidState }
        apply(value.order)
        if selectedID == id, currentOrder == value.order { detail = value }
        #if os(macOS)
        if let pending = uncertainRequest, pending.orderID == id {
            let finalReply = [.accepted, .working, .partial, .filled, .rejected, .cancelled].contains(value.order.state)
            let previewResolved = pending.operation == "preview" && [.draft, .proposed].contains(value.order.state)
            if finalReply || previewResolved { uncertainRequest = nil; uncertainty = nil }
        }
        #endif
    }
    private func apply(_ order: PaperOrder) {
        if let index = orders.firstIndex(where: { $0.id == order.id }) {
            let old = orders[index]
            guard order.version >= old.version, order.updatedAt >= old.updatedAt, order.filledQuantity >= old.filledQuantity else { return }
            guard old.state != .filled || order.state == .filled else { return }
            orders[index] = order
        } else { orders.append(order) }
        orders.sort { $0.createdAt > $1.createdAt }
        #if os(macOS)
        if let armed = armedConfirmation, currentOrder?.confirmation != armed { armedConfirmation = nil }
        #endif
    }
    private func recordReadError(_ error: Error) {
        guard !(error is CancellationError), !Task.isCancelled else { return }
        readError = error.localizedDescription
        if case ClientError.http(let status, _, _) = error, status == 401 { authorizationFailed = true }
    }

    #if os(macOS)
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
        guard canCreateDraft, let writer, let selection = accountSelection else { return }
        guard quantity.range(of: #"^[1-9][0-9]{0,5}$"#, options: .regularExpression) != nil, let count = Int(quantity) else {
            actionError = PaperOrderValidationError.invalidInput.localizedDescription; return
        }
        var sent = false
        let requestID = UUID()
        isWriting = true
        defer { isWriting = false }
        do {
            let request = try PaperOrderDraftRequest(clientRequestID: requestID, accountSelection: selection,
                symbol: symbol, side: side, quantity: count, limitPrice: limitPrice)
            try await fetchHealth()
            guard selection == accountSelection else { throw PaperOrderValidationError.targetUnavailable }
            sent = true
            let order = try await writer.createDraft(request)
            guard order.clientRequestID == requestID, order.accountSelection == selection else { throw PaperOrderValidationError.invalidState }
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
            guard updated.id == order.id else { throw PaperOrderValidationError.invalidState }
            apply(updated); armedConfirmation = nil; review = nil; actionError = nil
        } catch { writeFailed(error, operation: "preview", requestID: requestID, orderID: order.id, sent: sent) }
    }
    public func reviewConfirmation() {
        guard let order = currentOrder, canConfirm(order, now: clock()) else { return }
        do { review = PaperOrderReview(order: order, action: .confirm(try PaperOrderConfirmRequest(clientRequestID: UUID(), order: order, now: clock()))) }
        catch { actionError = error.localizedDescription }
    }
    public func reviewSubmission() {
        guard let order = currentOrder, canSubmit(order, now: clock()) else { return }
        do { review = PaperOrderReview(order: order, action: .submit(try PaperOrderSubmitRequest(clientRequestID: UUID(), order: order, now: clock()))) }
        catch { actionError = error.localizedDescription }
    }
    public func reviewCancellation() {
        guard let order = currentOrder, canCancel(order) else { return }
        do { review = PaperOrderReview(order: order, action: .cancel(try PaperOrderCancelRequest(clientRequestID: UUID(), order: order))) }
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
            case .cancel(let request):
                sent = true; updated = try await writer.cancel(pending.order.id, request: request)
            }
            guard updated.id == pending.order.id else { throw PaperOrderValidationError.invalidState }
            apply(updated)
            if case .confirm = pending.action, updated.state == .confirmed, let confirmation = updated.confirmation,
               confirmation.version == updated.version, confirmation.contentHash == updated.contentHash,
               updated.hasValidPreview(at: clock()) { armedConfirmation = confirmation }
            actionError = nil
        } catch {
            writeFailed(error, operation: pending.kind, requestID: pending.requestID, orderID: pending.order.id, sent: sent)
        }
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
        if case ClientError.http(let status, _, _) = error, status == 401 { authorizationFailed = true }
    }
    #endif
}

#if os(macOS)
public struct PaperOrderReview: Identifiable, Sendable {
    public let id = UUID()
    public let order: PaperOrder
    fileprivate let action: Action
    fileprivate enum Action: Sendable {
        case confirm(PaperOrderConfirmRequest), submit(PaperOrderSubmitRequest), cancel(PaperOrderCancelRequest)
    }
    fileprivate init(order: PaperOrder, action: Action) { self.order = order; self.action = action }
    public var kind: String {
        switch action { case .confirm: "confirm"; case .submit: "submit"; case .cancel: "cancel" }
    }
    public var title: String {
        switch action { case .confirm: "确认此 Paper 预览？"; case .submit: "提交此 Paper 订单？"; case .cancel: "撤销此 Paper 订单？" }
    }
    public var buttonTitle: String {
        switch action { case .confirm: "确认预览（不提交）"; case .submit: "提交 Paper 订单"; case .cancel: "确认撤销此单" }
    }
    fileprivate var requestID: UUID {
        switch action { case .confirm(let request): request.clientRequestID; case .submit(let request): request.clientRequestID; case .cancel(let request): request.clientRequestID }
    }
    public var summary: String {
        var text = "\(order.side.rawValue) \(order.contract.symbol) · \(order.quantity) 股 · 限价 \(order.limitPrice) USD\nSTK / SMART / USD · DAY / LMT"
        if let preview = order.preview {
            text += "\n初始保证金变动：\(preview.whatIf.initialMarginChange) USD\n维持保证金变动：\(preview.whatIf.maintenanceMarginChange) USD\n佣金估算：\(preview.whatIf.commission) USD"
        }
        return text
    }
}
#endif
