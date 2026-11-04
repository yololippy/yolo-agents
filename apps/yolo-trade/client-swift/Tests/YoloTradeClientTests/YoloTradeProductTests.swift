import Testing
@testable import YoloTradeClient

@Test func yoloTradeProductIdentityIsCentralized() {
    #expect(YoloTradeProduct.agentID == "yolo-trade")
    #expect(YoloTradeProduct.displayName == "YoloTrade")
}
