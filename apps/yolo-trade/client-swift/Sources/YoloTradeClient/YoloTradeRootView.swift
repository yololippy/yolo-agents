import SwiftUI
import YoloAgentsUI

public struct YoloTradeRootView: View {
    public init() {}
    public var body: some View {
        YoloAgentsRootView(identity: YoloTradeProduct.identity, features: [
            ChatFeature(id: "chat.paper-orders", title: "Paper 订单", systemImage: "list.bullet.rectangle") {
                PaperOrdersView(connection: $0)
            },
        ], messageAccessory: ChatMessageAccessory { message, connection in
            PaperOrderProposalCard(message: message, connection: connection)
        })
    }
}
