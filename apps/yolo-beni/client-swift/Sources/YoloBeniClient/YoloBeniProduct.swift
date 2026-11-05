import YoloAgentsSDK

public enum YoloBeniProduct {
    public static let agentID = "yolo-beni"
    public static let displayName = "YoloBeni"
    public static let keychainService = "com.yoloagents.yolo-beni.apple.v1"
    public static let defaultServiceAddress = "http://127.0.0.1:8766"
    public static let allowsLegacyServer = false
    public static let identity = AgentAppIdentity(
        agentID: agentID,
        appName: displayName,
        keychainService: keychainService,
        defaultServiceAddress: defaultServiceAddress,
        allowsLegacyServer: allowsLegacyServer
    )
}
