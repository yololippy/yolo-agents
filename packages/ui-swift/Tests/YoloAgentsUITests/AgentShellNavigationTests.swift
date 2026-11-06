import Foundation
import Testing
import SwiftUI
@testable import YoloAgentsUI

@Test func iphoneShellStartsAtConversationsWithAnActiveDestination() {
    let navigation = AgentShellNavigation()
    #expect(navigation.destination == .conversations)
    #expect(navigation.conversationPath.isEmpty)
}

@Test func iphoneDestinationsSwitchContentAndPreserveConversationDetail() {
    var navigation = AgentShellNavigation()
    let conversationID = UUID()
    navigation.conversationPath = [conversationID]
    let otherID = UUID()
    let image = DraftImage(data: Data([1, 2]), mediaType: "image/png", filename: "draft.png")
    navigation.drafts.set("未发送的消息", for: conversationID)
    navigation.drafts.setImages([image], for: conversationID)
    navigation.drafts.set("另一个会话的草稿", for: otherID)
    for destination: AgentShellDestination in [.feature("app.history"), .account, .settings, .conversations] {
        let selectionAccepted1 = navigation.select(destination, availableFeatureIDs: ["app.history"])
        #expect(selectionAccepted1)
        #expect(navigation.destination == destination)
        #expect(navigation.conversationPath == [conversationID])
        #expect(navigation.drafts.text(for: conversationID) == "未发送的消息")
        #expect(navigation.drafts.images(for: conversationID) == [image])
        #expect(navigation.drafts.text(for: otherID) == "另一个会话的草稿")
    }
    navigation.conversationPath.removeAll()
    let selectionAccepted2 = navigation.select(.settings, availableFeatureIDs: [])
    #expect(selectionAccepted2)
    let selectionAccepted3 = navigation.select(.conversations, availableFeatureIDs: [])
    #expect(selectionAccepted3)
    #expect(navigation.conversationPath.isEmpty)
}

@Test func unknownFeatureCannotReplaceContentOrLoseTheCurrentConversation() {
    var navigation = AgentShellNavigation()
    let conversationID = UUID()
    navigation.conversationPath = [conversationID]
    let selectionAccepted4 = navigation.select(.account, availableFeatureIDs: ["app.history"])
    #expect(selectionAccepted4)
    let selectionAccepted5 = !navigation.select(.feature("missing"), availableFeatureIDs: ["app.history"])
    #expect(selectionAccepted5)
    #expect(navigation.destination == .account)
    #expect(navigation.conversationPath == [conversationID])
    let selectionAccepted6 = !navigation.select(.feature(""), availableFeatureIDs: [])
    #expect(selectionAccepted6)
}

@Test func featureIdentifiersCannotCollideWithShellDestinations() {
    var navigation = AgentShellNavigation()
    let selectionAccepted7 = navigation.select(.feature("chat.account"), availableFeatureIDs: ["chat.account"])
    #expect(selectionAccepted7)
    #expect(navigation.destination == .feature("chat.account"))
    let selectionAccepted8 = navigation.select(.account, availableFeatureIDs: ["chat.account"])
    #expect(selectionAccepted8)
    #expect(navigation.destination == .account)
}

@Test func pagesOmitModalDismissalAndExistingSheetsKeepIt() {
    #expect(!AgentContentPresentation.page.showsDismissAction)
    #expect(AgentContentPresentation.modal.showsDismissAction)
}

@Test @MainActor func footerIncludesConversationsBeforeInjectedFeaturesAndAccountSettings() {
    let feature = ChatFeature(id: "app.history", title: "历史", systemImage: "clock") { _ in Text("历史") }
    let items = AgentShellNavigation.dockItems(features: [feature], configuration: .conversationFirst)
    #expect(items.map(\.id) == [.conversations, .feature("app.history"), .account, .settings])
    #expect(items.map(\.title) == ["会话", "历史", "ChatGPT 账号", "连接设置"])
    #expect(items.map(\.accessibilityIdentifier) == ["chat.conversations", "app.history", "chat.account", "chat.settings"])
    let custom = AgentShellNavigation.dockItems(features: [], configuration: .init(conversationsTitle: "对话"))
    #expect(custom.map(\.id) == [.conversations, .account, .settings])
    #expect(custom.first?.title == "对话")
}
