import SwiftUI
import YoloAgentsSDK
import YoloAgentsUI

public struct YoloBeniRootView: View {
    public init() {}
    public var body: some View {
        YoloAgentsRootView(identity: AgentAppIdentity(
            agentID: "yolo-beni", appName: "YoloBeni", keychainService: "com.yoloagents.yolo-beni.apple.v1",
            defaultServiceAddress: "http://127.0.0.1:8766"))
    }
}
