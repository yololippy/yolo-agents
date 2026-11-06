import SwiftUI
import YoloAgentsUI
import YoloAgentsSDK

public struct YoloTradeRootView: View {
    public init() {}
    static func paperOrdersPage(connection: PairedConnection) -> TradeView {
        #if os(iOS)
        TradeView(connection: connection, presentation: .page)
        #else
        TradeView(connection: connection)
        #endif
    }

    public var body: some View {
        YoloAgentsRootView(identity: YoloTradeProduct.identity, features: [
            ChatFeature(id: "chat.paper-orders", title: "交易", compactTitle: "交易",
                        systemImage: "chart.line.uptrend.xyaxis") {
                Self.paperOrdersPage(connection: $0)
            },
        ], messageAccessory: ChatMessageAccessory { message, connection in
            PaperOrderProposalCard(message: message, connection: connection)
        })
    }
}
