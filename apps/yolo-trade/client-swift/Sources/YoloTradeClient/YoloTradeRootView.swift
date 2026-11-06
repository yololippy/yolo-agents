import SwiftUI
import YoloAgentsUI
import YoloAgentsSDK

public struct YoloTradeRootView: View {
    public init() {}
    static func paperOrdersPage(connection: PairedConnection) -> PaperOrdersView {
        #if os(iOS)
        PaperOrdersView(connection: connection, presentation: .page)
        #else
        PaperOrdersView(connection: connection)
        #endif
    }

    public var body: some View {
        YoloAgentsRootView(identity: YoloTradeProduct.identity, features: [
            ChatFeature(id: "chat.paper-orders", title: "Paper 订单", compactTitle: "订单",
                        systemImage: "list.bullet.rectangle") {
                Self.paperOrdersPage(connection: $0)
            },
        ], messageAccessory: ChatMessageAccessory { message, connection in
            PaperOrderProposalCard(message: message, connection: connection)
        })
    }
}
