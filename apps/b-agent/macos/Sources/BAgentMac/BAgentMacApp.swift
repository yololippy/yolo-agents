import SwiftUI
import BAgentClient

@main
struct BAgentMacApp: App {
    var body: some Scene {
        Window("B Agent", id: "main") { BAgentRootView() }
    }
}
