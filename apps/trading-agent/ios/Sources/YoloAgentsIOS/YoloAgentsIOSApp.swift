import SwiftUI
import YoloAgentsUI

@main
struct YoloAgentsIOSApp: App {
    @ViewBuilder
    private var content: some View {
        #if DEBUG
        if ProcessInfo.processInfo.environment["YOLO_AGENTS_RUNNING_SDK_TESTS"] == "1" {
            Color.clear
        } else {
            YoloAgentsRootView()
        }
        #else
        YoloAgentsRootView()
        #endif
    }

    var body: some Scene {
        WindowGroup { content }
    }
}
