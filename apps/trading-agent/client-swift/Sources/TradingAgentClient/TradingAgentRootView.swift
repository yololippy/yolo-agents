import SwiftUI
import YoloAgentsUI
import YoloAgentsSDK

public struct TradingAgentRootView: View {
    public init() {}
    public var body: some View {
        YoloAgentsRootView(identity: AgentAppIdentity(
            agentID: "trading-agent", appName: "Trading Agent", keychainService: "com.tradingagent.apple.v1",
            defaultServiceAddress: "http://127.0.0.1:8765", allowsLegacyServer: true), features: [
            ChatFeature(id: "chat.paper-orders", title: "Paper 订单", systemImage: "list.bullet.rectangle") {
                PaperOrdersView(connection: $0)
            },
        ])
    }
}
