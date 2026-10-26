import SwiftUI
import YoloAgentsUI

@main
struct YoloAgentsMacApp: App {
    @ViewBuilder
    private var content: some View {
        #if DEBUG
        if ProcessInfo.processInfo.environment["YOLO_AGENTS_RUNNING_SDK_TESTS"] == "1" {
            // Hosted SDK tests must not restore production pairing or cached chat.
            Color.clear
        } else {
            YoloAgentsRootView()
        }
        #else
        YoloAgentsRootView()
        #endif
    }
    var body: some Scene {
        Window("Yolo Agents", id: "main") { content }
    }
}
