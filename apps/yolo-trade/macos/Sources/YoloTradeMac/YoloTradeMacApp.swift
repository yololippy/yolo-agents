import SwiftUI
import YoloTradeClient

@main
struct YoloTradeMacApp: App {
    @ViewBuilder
    private var content: some View {
        #if DEBUG
        if ProcessInfo.processInfo.environment["YOLO_AGENTS_RUNNING_SDK_TESTS"] == "1" {
            // Hosted SDK tests must not restore production pairing or cached chat.
            Color.clear
        } else {
            YoloTradeRootView()
        }
        #else
        YoloTradeRootView()
        #endif
    }
    var body: some Scene {
        Window(YoloTradeProduct.displayName, id: "main") { content }
    }
}
