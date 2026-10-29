import Foundation

public struct PaperOrderProposal: Equatable, Sendable {
    public enum Destination: Equatable, Sendable {
        case createDraft
        case existingDraft(UUID)
        case unavailable
    }

    public enum Kind: String, Sendable {
        case proposal = "paper_order_proposal"
        case draft = "paper_order_draft"

        var label: String { self == .proposal ? "订单提案" : "已创建 Paper 草稿" }
    }

    public let kind: Kind
    public let symbol: String?
    public let side: PaperOrderSide?
    public let quantity: Int?
    public let limitPrice: String?
    public let orderID: UUID?
    public let issues: [String]
    private let containsOrderID: Bool

    public var isComplete: Bool { issues.isEmpty }
    public var destination: Destination {
        switch kind {
        case .proposal where !containsOrderID: .createDraft
        case .draft:
            orderID.map(Destination.existingDraft) ?? .unavailable
        default: .unavailable
        }
    }

    /// Recognizes only an explicitly typed fenced JSON artifact. Ordinary prose
    /// and untyped JSON are never interpreted as an order instruction.
    public init?(messageContent: String) {
        guard let data = Self.typedArtifactData(in: messageContent),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rawKind = object["type"] as? String,
              let kind = Kind(rawValue: rawKind) else { return nil }

        self.kind = kind
        var issues: [String] = []

        containsOrderID = object.keys.contains("order_id")
        if containsOrderID, let rawOrderID = object["order_id"] as? String,
           let parsed = UUID(uuidString: rawOrderID), parsed.uuidString.lowercased() == rawOrderID.lowercased() {
            orderID = parsed
        } else {
            orderID = nil
        }
        if kind == .draft && !containsOrderID { issues.append("缺少已有草稿 ID") }
        else if containsOrderID && orderID == nil { issues.append("已有草稿 ID 无效") }
        else if kind == .proposal && containsOrderID { issues.append("新提案不能包含已有草稿 ID") }

        let rawSymbol = object["symbol"] as? String
        if let rawSymbol {
            symbol = try? PaperStockContract(symbol: rawSymbol).symbol
            if symbol == nil { issues.append("股票代码无效") }
        } else {
            symbol = nil; issues.append("缺少股票代码")
        }

        let rawSide = object["side"] as? String
        side = rawSide.flatMap { PaperOrderSide(rawValue: $0.uppercased()) }
        if rawSide == nil { issues.append("缺少买卖方向") }
        else if side == nil { issues.append("买卖方向必须是 BUY 或 SELL") }

        if let number = object["quantity"] as? NSNumber,
           CFGetTypeID(number) != CFBooleanGetTypeID(),
           number.doubleValue.rounded() == number.doubleValue,
           (1...100_000).contains(number.intValue) {
            quantity = number.intValue
        } else {
            quantity = nil
            issues.append(object["quantity"] == nil ? "缺少股数" : "股数必须是 1–100000 的整数")
        }

        if let price = object["limit_price"] as? String, PaperOrder.validLimitPrice(price) {
            limitPrice = price
        } else {
            limitPrice = nil
            issues.append(object["limit_price"] == nil ? "缺少限价" : "限价必须是有效的 USD 十进制字符串")
        }

        if let environment = object["environment"] as? String, environment != "paper" {
            issues.append("只支持 Paper 模拟环境")
        }
        if let orderType = object["order_type"] as? String, orderType != "LMT" {
            issues.append("只支持 LMT 限价单")
        }
        if let tif = object["tif"] as? String, tif != "DAY" {
            issues.append("只支持 DAY 当日单")
        }
        self.issues = issues
    }

    private static func typedArtifactData(in content: String) -> Data? {
        let pattern = #"```(?:paper-order|json)\s*([\s\S]*?)\s*```"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return nil }
        let range = NSRange(content.startIndex..<content.endIndex, in: content)
        for match in regex.matches(in: content, range: range) {
            guard let bodyRange = Range(match.range(at: 1), in: content),
                  let data = String(content[bodyRange]).data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let rawKind = object["type"] as? String,
                  Kind(rawValue: rawKind) != nil else { continue }
            return data
        }
        return nil
    }
}
