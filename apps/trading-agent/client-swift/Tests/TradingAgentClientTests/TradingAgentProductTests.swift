import Testing
@testable import TradingAgentClient

@Test func tradingProductIdentityIsCentralized() {
    #expect(TradingAgentProduct.agentID == "trading-agent")
    #expect(TradingAgentProduct.displayName == "有路交易")
    #expect(TradingAgentProduct.englishName == "Yolo Trade")
    #expect(TradingAgentProduct.displayNameWithEnglish == "有路交易 · Yolo Trade")
}
