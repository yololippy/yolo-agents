import SwiftUI
import TradingAgentUI

@main
struct TradingAgentApp: App {
    @ViewBuilder
    private var content: some View {
        #if DEBUG
        if ProcessInfo.processInfo.environment["TRADING_AGENT_RUNNING_SDK_TESTS"] == "1" {
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
        #if os(macOS)
        Window("Trading Agent", id: "main") { content }
        #else
        WindowGroup { content }
        #endif
    }
}
