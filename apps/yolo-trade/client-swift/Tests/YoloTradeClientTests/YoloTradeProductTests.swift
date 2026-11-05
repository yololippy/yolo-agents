import Testing
@testable import YoloTradeClient

@Test func yoloTradeProductIdentityIsCentralized() {
    #expect(YoloTradeProduct.agentID == "yolo-trade")
    #expect(YoloTradeProduct.displayName == "YoloTrade")
    #expect(YoloTradeProduct.keychainService == "com.yoloagents.yolo-trade.apple.v1")
    #expect(YoloTradeProduct.defaultServiceAddress == "http://127.0.0.1:8765")
    #expect(YoloTradeProduct.identity.agentID == YoloTradeProduct.agentID)
    #expect(YoloTradeProduct.identity.appName == YoloTradeProduct.displayName)
    #expect(YoloTradeProduct.identity.keychainService == YoloTradeProduct.keychainService)
    #expect(YoloTradeProduct.identity.defaultServiceAddress == YoloTradeProduct.defaultServiceAddress)
}
