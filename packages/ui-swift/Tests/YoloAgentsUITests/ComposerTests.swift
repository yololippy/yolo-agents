import Foundation
import Testing
@testable import YoloAgentsUI

@Test func reconnectOrSendingDoesNotDisableDraftEditing() {
    let offline = ComposerAvailability(hasConversation: true, archived: false, online: false, busy: false)
    #expect(offline.canEdit && !offline.canSend)
    let sending = ComposerAvailability(hasConversation: true, archived: false, online: true, busy: true)
    #expect(sending.canEdit && !sending.canSend)
    let archived = ComposerAvailability(hasConversation: true, archived: true, online: true, busy: false)
    #expect(!archived.canEdit && !archived.canSend)
}

@Test func draftsStayWithTheirConversationAndLateSendKeepsNewText() throws {
    var drafts = ComposerDrafts()
    let first = UUID(), second = UUID()
    drafts.set("第一条消息", for: first)
    drafts.set("另一个会话", for: second)
    let submission = try #require(drafts.submission(for: first))
    drafts.set("发送期间继续编辑", for: first)
    drafts.finish(submission, accepted: true)
    #expect(drafts.text(for: first) == "发送期间继续编辑")
    #expect(drafts.text(for: second) == "另一个会话")
}

@Test func onlyAcceptedUnchangedDraftIsCleared() throws {
    var drafts = ComposerDrafts()
    let id = UUID()
    drafts.set("你好", for: id)
    let submission = try #require(drafts.submission(for: id))
    drafts.finish(submission, accepted: false)
    #expect(drafts.text(for: id) == "你好")
    drafts.finish(submission, accepted: true)
    #expect(drafts.text(for: id).isEmpty)
    drafts.set(" \n", for: id)
    #expect(drafts.submission(for: id) == nil)
}
