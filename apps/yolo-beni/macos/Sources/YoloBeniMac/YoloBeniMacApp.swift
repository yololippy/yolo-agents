import SwiftUI
import YoloBeniClient

@main
struct YoloBeniMacApp: App {
    var body: some Scene {
        Window("YoloBeni", id: "main") { YoloBeniRootView() }
    }
}
