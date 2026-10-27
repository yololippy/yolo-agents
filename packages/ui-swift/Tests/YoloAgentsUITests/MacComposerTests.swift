#if os(macOS)
import AppKit
import Testing
@testable import YoloAgentsUI

@Test func returnPolicySendsOnlyCommittedUnmodifiedInput() {
    #expect(MacReturnAction.resolve(shift: false, markedText: false, canSend: true) == .send)
    #expect(MacReturnAction.resolve(shift: true, markedText: false, canSend: true) == .newline)
    #expect(MacReturnAction.resolve(shift: false, markedText: true, canSend: true) == .native)
    #expect(MacReturnAction.resolve(shift: true, markedText: true, canSend: true) == .native)
    #expect(MacReturnAction.resolve(shift: false, markedText: false, canSend: false) == .consume)
}

@Test @MainActor func nativeEditorUsesOneExplicitInsetAndFontForTextAndPlaceholder() {
    let view = MacComposerTextView(frame: NSRect(x: 0, y: 0, width: 320, height: 92))
    view.configureTextMetrics()
    #expect(view.textContainerInset == NSSize(width: 12, height: 10))
    #expect(view.textContainer?.lineFragmentPadding == 0)
    #expect(view.textContainerOrigin.x == 12 && view.textContainerOrigin.y == 10)
    #expect((view.typingAttributes[.font] as? NSFont) == view.font)
}

@Test @MainActor func nativeReturnSubmitsOnceAndShiftReturnInsertsNewline() throws {
    let view = MacComposerTextView(frame: NSRect(x: 0, y: 0, width: 320, height: 92))
    view.configureTextMetrics()
    view.string = "你好"
    view.setSelectedRange(NSRange(location: 2, length: 0))
    view.isEditable = true; view.canSubmit = true
    var submissions = 0
    view.onSubmit = { submissions += 1 }
    let enter = try #require(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
        windowNumber: 0, context: nil, characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: 36))
    view.keyDown(with: enter)
    #expect(submissions == 1 && view.string == "你好")
    let shifted = try #require(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .shift, timestamp: 0,
        windowNumber: 0, context: nil, characters: "\r", charactersIgnoringModifiers: "\r", isARepeat: false, keyCode: 36))
    view.keyDown(with: shifted)
    #expect(submissions == 1 && view.string == "你好\n")
}
#endif
