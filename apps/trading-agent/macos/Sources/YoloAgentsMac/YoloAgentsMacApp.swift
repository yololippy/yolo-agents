import SwiftUI
import TradingAgentClient

@main
struct YoloAgentsMacApp: App {
    @ViewBuilder
    private var content: some View {
        #if DEBUG
        if ProcessInfo.processInfo.environment["YOLO_AGENTS_RUNNING_SDK_TESTS"] == "1" {
            // Hosted SDK tests must not restore production pairing or cached chat.
            Color.clear
        } else {
            TradingAgentRootView()
        }
        #else
        TradingAgentRootView()
        #endif
    }
    var body: some Scene {
        Window("Trading Agent", id: "main") { content }
    }
}
