import Foundation
import Testing
import YoloAgentsSDK
@testable import YoloAgentsUI

@Test func composerCopyCanBeCustomizedPerAgentWithoutForkingTheUI() {
    let standard = AgentComposerConfiguration.standard
    #expect(standard.placeholder == "输入消息…")
    let custom = AgentComposerConfiguration(placeholder: "询问助手…",
                                            offlinePlaceholder: "离线时保存草稿",
                                            archivedPlaceholder: "已归档")
    #expect(custom.placeholder == "询问助手…")
    #expect(custom.offlinePlaceholder == "离线时保存草稿")
    #expect(custom.archivedPlaceholder == "已归档")
}

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

@Test func imageOnlyDraftsSubmitAndLateChangesArePreserved() throws {
    var drafts = ComposerDrafts()
    let id = UUID()
    let first = DraftImage(data: Data([1, 2, 3]), mediaType: "image/jpeg", filename: "first.jpg")
    let second = DraftImage(data: Data([4, 5]), mediaType: "image/png", filename: "second.png")
    drafts.setImages([first], for: id)
    let submission = try #require(drafts.submission(for: id))
    #expect(submission.content.isEmpty)
    #expect(submission.images == [first])
    drafts.setImages([first, second], for: id)
    drafts.finish(submission, accepted: true)
    #expect(drafts.images(for: id) == [first, second])
}

@Test func imagePreparationUsesTheBytesActualTypeInsteadOfPickerHint() throws {
    let png = try #require(Data(base64Encoded:
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
    let image = try DraftImage.preparing(data: png, sourceMediaType: "image/heic")
    #expect(image.mediaType == "image/png")
    #expect(image.data == png)
}
