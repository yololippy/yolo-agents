import SwiftUI
import YoloAgentsUI

public struct TradingAgentRootView: View {
    public init() {}
    public var body: some View {
        YoloAgentsRootView(features: [
            ChatFeature(id: "chat.paper-orders", title: "Paper 订单", systemImage: "list.bullet.rectangle") {
                PaperOrdersView(connection: $0)
            },
        ])
    }
}
