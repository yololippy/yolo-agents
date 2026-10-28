import SwiftUI
import YoloAgentsSDK
import YoloAgentsUI

public struct BAgentRootView: View {
    public init() {}
    public var body: some View {
        YoloAgentsRootView(identity: AgentAppIdentity(
            agentID: "b-agent", appName: "B Agent", keychainService: "com.yoloagents.b-agent.apple.v1",
            defaultServiceAddress: "http://127.0.0.1:8766"))
    }
}
