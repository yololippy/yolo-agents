import Foundation
import YoloAgentsSDK

/// Product-private contracts. Unknown asset classes remain readable and cannot create tickets.
public struct TradeInstrument: Codable, Equatable, Identifiable, Sendable {
    public let conID: Int
    public let symbol: String
    public let name: String?
    public let secType: String
    public let exchange: String?
    public let primaryExchange: String?
    public let currency: String
    public var id: Int { conID }
    enum CodingKeys: String, CodingKey {
        case symbol, name, exchange, currency
        case conID = "con_id", secType = "sec_type", primaryExchange = "primary_exchange"
    }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        conID = try values.decode(Int.self, forKey: .conID)
        symbol = try values.decode(String.self, forKey: .symbol)
        name = try values.decodeIfPresent(String.self, forKey: .name)
        secType = try values.decode(String.self, forKey: .secType)
        exchange = try values.decodeIfPresent(String.self, forKey: .exchange)
        primaryExchange = try values.decodeIfPresent(String.self, forKey: .primaryExchange)
        currency = try values.decode(String.self, forKey: .currency)
        guard conID > 0, !symbol.isEmpty, !secType.isEmpty, !currency.isEmpty else {
            throw PaperOrderValidationError.invalidInput
        }
    }
    public var quickContract: PaperStockContract? {
        guard secType == "STK", exchange == "SMART", currency == "USD", let primaryExchange else { return nil }
        return try? PaperStockContract(symbol: symbol, conID: conID, primaryExchange: primaryExchange)
    }
}

public struct TradePosition: Decodable, Equatable, Identifiable, Sendable {
    public let contract: TradeInstrument
    public let quantity: String?
    public let averageCost: String?
    public let marketPrice: String?
    public let marketValue: String?
    public let unrealizedPNL: String?
    public let asOf: Date?
    public var id: Int { contract.conID }
    enum CodingKeys: String, CodingKey {
        case quantity
        case averageCost = "average_cost", marketPrice = "market_price", marketValue = "market_value"
        case unrealizedPNL = "unrealized_pnl", asOf = "as_of"
    }
    public init(from decoder: Decoder) throws {
        contract = try TradeInstrument(from: decoder)
        let values = try decoder.container(keyedBy: CodingKeys.self)
        quantity = try values.decodeIfPresent(String.self, forKey: .quantity)
        averageCost = try values.decodeIfPresent(String.self, forKey: .averageCost)
        marketPrice = try values.decodeIfPresent(String.self, forKey: .marketPrice)
        marketValue = try values.decodeIfPresent(String.self, forKey: .marketValue)
        unrealizedPNL = try values.decodeIfPresent(String.self, forKey: .unrealizedPNL)
        asOf = try values.decodeIfPresent(Date.self, forKey: .asOf)
    }

}

public struct TradeQuote: Decodable, Equatable, Sendable {
    public let conID: Int
    public let bid: String?
    public let ask: String?
    public let last: String?
    public let dataType: String
    public let asOf: Date?
    enum CodingKeys: String, CodingKey { case bid, ask, last; case conID = "con_id", dataType = "data_type", asOf = "as_of" }
}

public struct TradeDeviceAuthorization: Decodable, Equatable, Identifiable, Sendable {
    public let deviceID: UUID
    public let paperTradingEnabled: Bool
    public let authorizationRevision: String?
    public var id: UUID { deviceID }
    public var permitsPaper: Bool { paperTradingEnabled }
    public var permitsPhonePaper: Bool {
        paperTradingEnabled && authorizationRevision?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
    }
    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id", paperTradingEnabled = "paper_trading_enabled", authorizationRevision = "authorization_revision"
    }
}

/// The Host returns only paired iPhones from this Mac-only endpoint.
public struct TradePairedPhone: Decodable, Equatable, Identifiable, Sendable {
    public let deviceID: UUID
    public let name: String
    public let revokedAt: Date?
    public let paperTradingEnabled: Bool
    public let authorizationRevision: String?
    public var id: UUID { deviceID }
    public var permitsPaper: Bool {
        paperTradingEnabled && revokedAt == nil && authorizationRevision?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false
    }
    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id", name = "device_name", revokedAt = "revoked_at", paperTradingEnabled = "paper_trading_enabled", authorizationRevision = "authorization_revision"
    }
}
struct TradePositionsResponse: Decodable, Sendable {
    let positions: [TradePosition]
    let count: Int
    let asOf: Date
    enum CodingKeys: String, CodingKey { case positions, count; case asOf = "as_of" }
}

public enum TradePage: String, CaseIterable, Sendable {
    case positions, search, orders
    public var title: String { switch self { case .positions: "持仓"; case .search: "搜索"; case .orders: "订单" } }
}

/// Quotes have no entry point into ticket price state. Only typed user input supplies the limit.
public struct PaperQuickTicket: Equatable, Sendable {
    public let contract: PaperStockContract
    public var side: PaperOrderSide
    public var quantity = ""
    public var limitPrice = ""
    public init(contract: PaperStockContract, side: PaperOrderSide) {
        self.contract = contract; self.side = side
    }
    public var isValid: Bool {
        contract.conID != nil && contract.primaryExchange != nil &&
        quantity.range(of: #"^[1-9][0-9]{0,5}$"#, options: .regularExpression) != nil &&
        Int(quantity).map { (1...100_000).contains($0) } == true && PaperOrder.validLimitPrice(limitPrice)
    }
}
