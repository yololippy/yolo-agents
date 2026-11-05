import Testing
@testable import YoloBeniClient

@Test func yoloBeniProductIdentityIsCentralized() {
    #expect(YoloBeniProduct.agentID == "yolo-beni")
    #expect(YoloBeniProduct.displayName == "YoloBeni")
    #expect(YoloBeniProduct.keychainService == "com.yoloagents.yolo-beni.apple.v1")
    #expect(YoloBeniProduct.defaultServiceAddress == "http://127.0.0.1:8766")
    #expect(YoloBeniProduct.identity.agentID == YoloBeniProduct.agentID)
    #expect(YoloBeniProduct.identity.appName == YoloBeniProduct.displayName)
    #expect(YoloBeniProduct.identity.keychainService == YoloBeniProduct.keychainService)
    #expect(YoloBeniProduct.identity.defaultServiceAddress == YoloBeniProduct.defaultServiceAddress)
}
