import SwiftUI
import YoloTradeClient

@main
struct YoloTradeIOSApp: App {
    @ViewBuilder
    private var content: some View {
        #if DEBUG
        if ProcessInfo.processInfo.environment["YOLO_AGENTS_RUNNING_SDK_TESTS"] == "1" {
            Color.clear
        } else {
            YoloTradeRootView()
        }
        #else
        YoloTradeRootView()
        #endif
    }

    var body: some Scene {
        WindowGroup { content }
    }
}
