import Foundation
import YoloAgentsSDK

public enum PaperOrderValidationError: Error, LocalizedError, Equatable, Sendable {
    case invalidInput, targetUnavailable, executionUnavailable, invalidState, expired, permissionDenied
    public var errorDescription: String? {
        switch self {
        case .invalidInput: "请填写有效股票代码、1–100000 的整数数量和最多四位小数的正限价。"
        case .targetUnavailable: "服务端尚未提供已配置的 Paper 账户选择，不能创建草稿。"
        case .executionUnavailable: "Paper 执行门槛尚未全部就绪，请刷新服务状态。"
        case .invalidState: "订单状态或绑定内容已变化，请重新读取订单。"
        case .expired: "预览或确认已过期，请重新创建草稿。"
        case .permissionDenied: "仅有效的 Mac 管理设备可操作 Paper 订单。"
        }
    }
}

public struct PaperBrokerConfiguration: Codable, Equatable, Sendable {
    public let configID: String?
    public let environment: String?
    public let accountConfigured: Bool?
    public let readOnly: Bool?
    public let executionEnabled: Bool?
    enum CodingKeys: String, CodingKey {
        case environment
        case configID = "config_id", accountConfigured = "account_configured"
        case readOnly = "read_only", executionEnabled = "execution_enabled"
    }
}
public struct PaperBrokerHealth: Codable, Equatable, Sendable {
    public let configuration: PaperBrokerConfiguration?
    public let state: String?
    public let connected: Bool?
    public let executionEnabled: Bool?
    public let environmentVerified: Bool?
    public let dataComplete: Bool?
    public let accountVerified: Bool?
    public let permissionsVerified: Bool?
    public let healthStale: Bool?
    public let sessionID: String?
    public let blockers: [ServiceError]?
    enum CodingKeys: String, CodingKey {
        case configuration, state, connected, blockers
        case executionEnabled = "execution_enabled", environmentVerified = "environment_verified"
        case dataComplete = "data_complete", accountVerified = "account_verified"
        case permissionsVerified = "permissions_verified", healthStale = "health_stale", sessionID = "session_id"
    }
}

public struct PaperAccountSelection: Codable, Equatable, Hashable, Sendable {
    public let rawValue: String
    private init(_ value: String) throws {
        guard value.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil else {
            throw PaperOrderValidationError.targetUnavailable
        }
        rawValue = value
    }
    public init(health: PaperHealth) throws {
        guard let broker = health.broker else { throw PaperOrderValidationError.targetUnavailable }
        try self.init(broker: broker)
    }
    public init(broker: PaperBrokerHealth) throws {
        guard let configuration = broker.configuration,
              configuration.environment == "paper", configuration.accountConfigured == true,
              let value = configuration.configID else { throw PaperOrderValidationError.targetUnavailable }
        try self.init(value)
    }
    public init(from decoder: Decoder) throws { try self.init(decoder.singleValueContainer().decode(String.self)) }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer(); try container.encode(rawValue)
    }
}

public struct OrderContentHash: Codable, Equatable, Hashable, Sendable {
    public let rawValue: String
    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        guard value.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil else { throw PaperOrderValidationError.invalidState }
        rawValue = value
    }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer(); try container.encode(rawValue)
    }
}

/// Keep exact server spelling: the backend binds expires_at by string equality.
public struct OrderExpiry: Codable, Equatable, Sendable {
    public let rawValue: String
    public let date: Date
    public init(from decoder: Decoder) throws {
        rawValue = try decoder.singleValueContainer().decode(String.self)
        date = try WireJSON.decoder().decode(Date.self, from: JSONEncoder().encode(rawValue))
    }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer(); try container.encode(rawValue)
    }
}

public enum PaperOrderSide: String, Codable, CaseIterable, Sendable { case buy = "BUY", sell = "SELL" }
public struct PaperStockContract: Codable, Equatable, Sendable {
    public let symbol: String
    public let secType: String
    public let exchange: String
    public let currency: String
    public init(symbol: String) throws {
        let normalized = symbol.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard normalized.range(of: #"^[A-Z][A-Z0-9.]{0,14}$"#, options: .regularExpression) != nil else { throw PaperOrderValidationError.invalidInput }
        self.symbol = normalized; secType = "STK"; exchange = "SMART"; currency = "USD"
    }
    enum CodingKeys: String, CodingKey { case symbol, exchange, currency; case secType = "sec_type" }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let symbol = try values.decode(String.self, forKey: .symbol)
        guard try values.decode(String.self, forKey: .secType) == "STK",
              try values.decode(String.self, forKey: .exchange) == "SMART",
              try values.decode(String.self, forKey: .currency) == "USD" else { throw PaperOrderValidationError.invalidInput }
        try self.init(symbol: symbol)
    }
}
public struct QualifiedPaperContract: Codable, Equatable, Sendable {
    public let conID: Int
    public let symbol: String
    public let secType: String
    public let exchange: String
    public let currency: String
    enum CodingKeys: String, CodingKey { case symbol, exchange, currency; case conID = "con_id", secType = "sec_type" }
    public func matches(_ contract: PaperStockContract) -> Bool {
        conID > 0 && symbol == contract.symbol && secType == contract.secType && exchange == contract.exchange && currency == contract.currency
    }
}
public struct PaperWhatIf: Codable, Equatable, Sendable {
    public let complete: Bool
    public let accepted: Bool
    public let currency: String
    public let initialMarginChange: String
    public let maintenanceMarginChange: String
    public let commission: String
    enum CodingKeys: String, CodingKey {
        case complete, accepted, currency, commission
        case initialMarginChange = "initial_margin_change", maintenanceMarginChange = "maintenance_margin_change"
    }
    public var isComplete: Bool {
        let pattern = #"^-?(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,4})?$"#
        return complete && accepted && currency == "USD" &&
            [initialMarginChange, maintenanceMarginChange, commission].allSatisfy { $0.range(of: pattern, options: .regularExpression) != nil } &&
            Decimal(string: commission, locale: Locale(identifier: "en_US_POSIX")).map { $0 >= 0 } == true
    }
}
public struct PaperOrderPreview: Codable, Equatable, Sendable {
    public let id: UUID
    public let qualifiedContract: QualifiedPaperContract
    public let whatIf: PaperWhatIf
    public let expiresAt: OrderExpiry
    enum CodingKeys: String, CodingKey { case id; case qualifiedContract = "qualified_contract", whatIf = "what_if", expiresAt = "expires_at" }
}
public struct PaperOrderConfirmation: Codable, Equatable, Sendable {
    public let id: UUID
    public let version: Int
    public let contentHash: OrderContentHash
    public let expiresAt: OrderExpiry
    enum CodingKeys: String, CodingKey { case id, version; case contentHash = "content_hash", expiresAt = "expires_at" }
}

public struct PaperOrderState: RawRepresentable, Codable, Equatable, Hashable, Sendable {
    public let rawValue: String
    public init(rawValue: String) { self.rawValue = rawValue }
    public init(from decoder: Decoder) throws { rawValue = try decoder.singleValueContainer().decode(String.self) }
    public func encode(to encoder: Encoder) throws { var value = encoder.singleValueContainer(); try value.encode(rawValue) }
    public static let draft = Self(rawValue: "draft"), proposed = Self(rawValue: "proposed"), confirmed = Self(rawValue: "confirmed")
    public static let previewing = Self(rawValue: "previewing"), submitting = Self(rawValue: "submitting")
    public static let accepted = Self(rawValue: "accepted"), working = Self(rawValue: "working"), partial = Self(rawValue: "partial")
    public static let filled = Self(rawValue: "filled"), rejected = Self(rawValue: "rejected"), unknown = Self(rawValue: "unknown")
    public static let cancelPending = Self(rawValue: "cancel_pending"), cancelled = Self(rawValue: "cancelled")
    public var canCancel: Bool { [.accepted, .working, .partial].contains(self) }
    public var isUnresolved: Bool { [.submitting, .cancelPending, .unknown].contains(self) }
    public var label: String {
        switch self {
        case .draft: "草稿"
        case .previewing: "预览处理中"
        case .proposed: "等待确认预览"
        case .confirmed: "已确认，尚未提交"
        case .submitting: "提交结果待确认"
        case .accepted: "已接受"
        case .working: "挂单中"
        case .partial: "部分成交"
        case .filled: "已成交"
        case .rejected: "已拒绝"
        case .unknown: "结果不明 · 需要对账"
        case .cancelPending: "撤单处理中"
        case .cancelled: "已撤单"
        default: "未识别状态：\(rawValue)"
        }
    }
}

public struct PaperOrder: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let clientRequestID: UUID
    public let environment: String
    public let accountSelection: PaperAccountSelection
    public let contract: PaperStockContract
    public let side: PaperOrderSide
    public let quantity: Int
    public let limitPrice: String
    public let orderType: String
    public let tif: String
    public let version: Int
    public let contentHash: OrderContentHash
    public let state: PaperOrderState
    public let preview: PaperOrderPreview?
    public let confirmation: PaperOrderConfirmation?
    public let filledQuantity: Int
    public let averageFillPrice: String?
    public let errorCode: String?
    public let createdAt: Date
    public let updatedAt: Date
    enum CodingKeys: String, CodingKey {
        case id, environment, contract, side, quantity, tif, version, state, preview, confirmation
        case clientRequestID = "client_request_id", accountSelection = "account_selection", limitPrice = "limit_price"
        case orderType = "order_type", contentHash = "content_hash", filledQuantity = "filled_quantity"
        case averageFillPrice = "average_fill_price", errorCode = "error_code", createdAt = "created_at", updatedAt = "updated_at"
    }
    public var hasSupportedShape: Bool {
        environment == "paper" && orderType == "LMT" && tif == "DAY" && version > 0 &&
            (1...100_000).contains(quantity) && (0...quantity).contains(filledQuantity) && Self.validLimitPrice(limitPrice)
    }
    public static func validLimitPrice(_ value: String) -> Bool {
        guard value.range(of: #"^(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,4})?$"#, options: .regularExpression) != nil,
              let price = Decimal(string: value, locale: Locale(identifier: "en_US_POSIX")) else { return false }
        return price > 0 && price <= 1_000_000
    }
    public func hasValidPreview(at now: Date) -> Bool {
        guard hasSupportedShape, let preview else { return false }
        return preview.expiresAt.date > now && preview.qualifiedContract.matches(contract) && preview.whatIf.isComplete
    }
}
public struct PaperOrderEvent: Codable, Equatable, Sendable, Identifiable {
    public let seq: Int64
    public let type: String
    public let state: PaperOrderState?
    public let errorCode: String?
    public let createdAt: Date
    public var id: Int64 { seq }
    enum CodingKeys: String, CodingKey { case seq, type, state; case errorCode = "error_code", createdAt = "created_at" }
}
public struct PaperOrderDetail: Codable, Equatable, Sendable {
    public let order: PaperOrder
    public let events: [PaperOrderEvent]
}
public struct PaperOrderResponse: Codable, Equatable, Sendable { public let order: PaperOrder }

extension PaperHealth {
    public var paperExecutionReady: Bool {
        guard tradeExecutionEnabled, let broker, let configuration = broker.configuration,
              (try? PaperAccountSelection(health: self)) != nil else { return false }
        return configuration.readOnly == false && configuration.executionEnabled == true &&
            broker.connected == true && broker.executionEnabled == true && broker.environmentVerified == true &&
            broker.accountVerified == true && broker.permissionsVerified == true && broker.dataComplete == true &&
            broker.healthStale == false && broker.sessionID?.isEmpty == false
    }
}
