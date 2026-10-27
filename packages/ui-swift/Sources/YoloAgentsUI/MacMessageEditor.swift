#if os(macOS)
import AppKit
import SwiftUI

enum MacReturnAction: Equatable {
    case send, newline, native, consume
    static func resolve(shift: Bool, markedText: Bool, canSend: Bool) -> MacReturnAction {
        if markedText { return .native }
        if shift { return .newline }
        return canSend ? .send : .consume
    }
}

/// Draw both text and placeholder through AppKit layout with identical metrics.
final class MacComposerTextView: NSTextView {
    var placeholder = "" { didSet { if placeholder != oldValue { needsDisplay = true } } }
    var canSubmit = false
    var onSubmit: () -> Void = {}

    func configureTextMetrics() {
        font = NSFont.preferredFont(forTextStyle: .body)
        textContainerInset = NSSize(width: 12, height: 10)
        textContainer?.lineFragmentPadding = 0
        defaultParagraphStyle = NSParagraphStyle.default
        typingAttributes = [.font: font!, .foregroundColor: NSColor.labelColor,
                            .paragraphStyle: NSParagraphStyle.default]
    }
    override func didChangeText() { super.didChangeText(); needsDisplay = true }
    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty, let textContainer, let font else { return }
        let storage = NSTextStorage(string: placeholder, attributes: [
            .font: font, .foregroundColor: NSColor.placeholderTextColor,
            .paragraphStyle: defaultParagraphStyle ?? NSParagraphStyle.default,
        ])
        let layout = NSLayoutManager()
        let container = NSTextContainer(containerSize: textContainer.containerSize)
        container.lineFragmentPadding = textContainer.lineFragmentPadding
        layout.addTextContainer(container)
        storage.addLayoutManager(layout)
        layout.ensureLayout(for: container)
        layout.drawGlyphs(forGlyphRange: layout.glyphRange(for: container), at: textContainerOrigin)
    }
    override func keyDown(with event: NSEvent) {
        guard event.keyCode == 36 || event.keyCode == 76 else { super.keyDown(with: event); return }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if !flags.intersection([.control, .option, .command]).isEmpty { super.keyDown(with: event); return }
        switch MacReturnAction.resolve(shift: flags.contains(.shift), markedText: hasMarkedText(),
                                      canSend: isEditable && canSubmit && !string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
        case .send: onSubmit()
        case .newline: insertNewlineIgnoringFieldEditor(nil)
        case .native: super.keyDown(with: event)
        case .consume: break
        }
    }
}

struct MacMessageEditor: NSViewRepresentable {
    @Binding var text: String
    let placeholder: String
    let isEditable: Bool
    let canSubmit: Bool
    let focusRequest: Int
    let onSubmit: () -> Void

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 240, height: 92))
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        let storage = NSTextStorage()
        let layout = NSLayoutManager()
        let container = NSTextContainer(containerSize: NSSize(width: scroll.contentSize.width, height: CGFloat.greatestFiniteMagnitude))
        storage.addLayoutManager(layout)
        layout.addTextContainer(container)
        let editor = MacComposerTextView(frame: NSRect(origin: .zero, size: scroll.contentSize), textContainer: container)
        editor.isRichText = false
        editor.importsGraphics = false
        editor.drawsBackground = false
        editor.allowsUndo = true
        editor.isVerticallyResizable = true
        editor.isHorizontallyResizable = false
        editor.minSize = NSSize(width: 0, height: 92)
        editor.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        editor.autoresizingMask = [.width]
        editor.textContainer?.widthTracksTextView = true
        editor.textContainer?.heightTracksTextView = false
        editor.configureTextMetrics()
        editor.delegate = context.coordinator
        editor.setAccessibilityLabel("消息输入")
        editor.setAccessibilityIdentifier("chat.composer")
        scroll.documentView = editor
        updateNSView(scroll, context: context)
        return scroll
    }
    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let editor = scroll.documentView as? MacComposerTextView else { return }
        context.coordinator.parent = self
        if editor.string != text && !editor.hasMarkedText() {
            let selection = editor.selectedRange()
            editor.string = text
            let length = (text as NSString).length
            let location = min(selection.location, length)
            editor.setSelectedRange(NSRange(location: location, length: min(selection.length, length - location)))
            editor.configureTextMetrics()
            editor.needsDisplay = true
        }
        editor.placeholder = placeholder
        editor.isEditable = isEditable
        editor.isSelectable = true
        editor.canSubmit = canSubmit
        editor.onSubmit = onSubmit
        if context.coordinator.lastFocusRequest != focusRequest {
            context.coordinator.lastFocusRequest = focusRequest
            DispatchQueue.main.async { [weak editor] in
                guard let editor, editor.isEditable else { return }
                editor.window?.makeFirstResponder(editor)
            }
        }
    }
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: MacMessageEditor
        var lastFocusRequest = 0
        init(_ parent: MacMessageEditor) { self.parent = parent }
        func textDidChange(_ notification: Notification) {
            guard let editor = notification.object as? MacComposerTextView else { return }
            parent.text = editor.string
        }
    }
}
#endif
