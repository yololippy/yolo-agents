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
        if event.keyCode == 53 {
            window?.makeFirstResponder(nil)
            return
        }
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
    @Binding var measuredHeight: CGFloat
    let placeholder: String
    let isEditable: Bool
    let canSubmit: Bool
    let focusRequest: Int
    let dismissRequest: Int
    let onSubmit: () -> Void
    private let minimumHeight: CGFloat = 40
    private let maximumHeight: CGFloat = 136

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 240, height: minimumHeight))
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = false
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
        editor.minSize = NSSize(width: 0, height: minimumHeight)
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
        context.coordinator.updateHeight(editor, in: scroll)
        if context.coordinator.lastFocusRequest != focusRequest {
            context.coordinator.lastFocusRequest = focusRequest
            DispatchQueue.main.async { [weak editor] in
                guard let editor, editor.isEditable else { return }
                editor.window?.makeFirstResponder(editor)
            }
        }
        if context.coordinator.lastDismissRequest != dismissRequest {
            context.coordinator.lastDismissRequest = dismissRequest
            DispatchQueue.main.async { [weak editor] in
                guard let editor else { return }
                if editor.window?.firstResponder === editor { editor.window?.makeFirstResponder(nil) }
            }
        }
    }
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    @MainActor final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: MacMessageEditor
        var lastFocusRequest = 0
        var lastDismissRequest = 0
        init(_ parent: MacMessageEditor) { self.parent = parent }
        func textDidChange(_ notification: Notification) {
            guard let editor = notification.object as? MacComposerTextView else { return }
            parent.text = editor.string
            guard let scroll = editor.enclosingScrollView else { return }
            updateHeight(editor, in: scroll)
        }
        func updateHeight(_ editor: MacComposerTextView, in scroll: NSScrollView) {
            guard scroll.contentSize.width > 0,
                  let textContainer = editor.textContainer,
                  let layoutManager = editor.layoutManager else { return }
            textContainer.containerSize = NSSize(width: max(1, scroll.contentSize.width),
                                                 height: .greatestFiniteMagnitude)
            layoutManager.ensureLayout(for: textContainer)
            let natural = ceil(layoutManager.usedRect(for: textContainer).height +
                               editor.textContainerInset.height * 2)
            let height = min(max(natural, parent.minimumHeight), parent.maximumHeight)
            scroll.hasVerticalScroller = natural > parent.maximumHeight
            if abs(parent.measuredHeight - height) > 0.5 {
                DispatchQueue.main.async { [weak self] in self?.parent.measuredHeight = height }
            }
        }
    }
}
#endif
