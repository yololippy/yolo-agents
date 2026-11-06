import YoloAgentsSDK

public enum YoloTradeProduct {
    public static let agentID = "yolo-trade"
    public static let displayName = "YoloTrade"
    public static let keychainService = "com.yoloagents.yolo-trade.apple.v1"
    public static let defaultServiceAddress = "http://127.0.0.1:8765"
    static let prTestBundleIdentifierPrefix = "com.yoloagents.yolotrade.prtest."
    public static let identity = AgentAppIdentity(
        agentID: agentID,
        appName: displayName,
        keychainService: keychainService,
        defaultServiceAddress: defaultServiceAddress
    )
}
