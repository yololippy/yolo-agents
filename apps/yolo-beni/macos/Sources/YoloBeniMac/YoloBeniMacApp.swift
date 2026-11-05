import SwiftUI
import YoloBeniClient

@main
struct YoloBeniMacApp: App {
    var body: some Scene {
        Window(YoloBeniProduct.displayName, id: "main") { YoloBeniRootView() }
    }
}
