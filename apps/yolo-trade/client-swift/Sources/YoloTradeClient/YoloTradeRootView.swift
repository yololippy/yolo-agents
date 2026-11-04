import SwiftUI
import YoloAgentsUI
import YoloAgentsSDK

public struct YoloTradeRootView: View {
    public init() {}
    public var body: some View {
        YoloAgentsRootView(identity: AgentAppIdentity(
            agentID: YoloTradeProduct.agentID, appName: YoloTradeProduct.displayName,
            keychainService: "com.yoloagents.yolo-trade.apple.v1",
            defaultServiceAddress: "http://127.0.0.1:8765", allowsLegacyServer: true), features: [
            ChatFeature(id: "chat.paper-orders", title: "Paper 订单", systemImage: "list.bullet.rectangle") {
                PaperOrdersView(connection: $0)
            },
        ], messageAccessory: ChatMessageAccessory { message, connection in
            PaperOrderProposalCard(message: message, connection: connection)
        })
    }
}
