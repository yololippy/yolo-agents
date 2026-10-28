import SwiftUI
import TradingAgentClient

@main
struct YoloAgentsIOSApp: App {
    @ViewBuilder
    private var content: some View {
        #if DEBUG
        if ProcessInfo.processInfo.environment["YOLO_AGENTS_RUNNING_SDK_TESTS"] == "1" {
            Color.clear
        } else {
            TradingAgentRootView()
        }
        #else
        TradingAgentRootView()
        #endif
    }

    var body: some Scene {
        WindowGroup { content }
    }
}
