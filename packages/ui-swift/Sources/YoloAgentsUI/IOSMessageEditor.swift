#if os(iOS)
import SwiftUI
import UIKit

/// A native multiline editor so the shared composer gets reliable intrinsic
/// sizing, Chinese marked-text handling, and explicit keyboard dismissal.
final class IOSComposerTextView: UITextView {
    var placeholder = "" {
        didSet { if placeholder != oldValue { setNeedsDisplay() } }
    }

    func configureTextMetrics() {
        backgroundColor = .clear
        font = .preferredFont(forTextStyle: .body)
        textColor = .label
        tintColor = .tintColor
        textContainerInset = UIEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        textContainer.lineFragmentPadding = 0
        adjustsFontForContentSizeCategory = true
        autocorrectionType = .default
        autocapitalizationType = .sentences
        keyboardDismissMode = .interactive
    }

    override var text: String! {
        didSet { setNeedsDisplay(); invalidateIntrinsicContentSize() }
    }

    override func draw(_ rect: CGRect) {
        super.draw(rect)
        guard text.isEmpty, !placeholder.isEmpty, let font else { return }
        let origin = CGPoint(x: textContainerInset.left + textContainer.lineFragmentPadding,
                             y: textContainerInset.top)
        (placeholder as NSString).draw(at: origin, withAttributes: [
            .font: font,
            .foregroundColor: UIColor.placeholderText,
        ])
    }
}

struct IOSMessageEditor: UIViewRepresentable {
    @Binding var text: String
    @Binding var measuredHeight: CGFloat
    let placeholder: String
    let isEditable: Bool
    let focusRequest: Int
    let dismissRequest: Int
    private let minimumHeight: CGFloat = 40
    private let maximumHeight: CGFloat = 136

    func makeUIView(context: Context) -> IOSComposerTextView {
        let editor = IOSComposerTextView()
        editor.configureTextMetrics()
        editor.delegate = context.coordinator
        editor.isScrollEnabled = false
        editor.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        editor.accessibilityLabel = "消息输入"
        editor.accessibilityIdentifier = "chat.composer"

        let toolbar = UIToolbar()
        toolbar.sizeToFit()
        let flexible = UIBarButtonItem(systemItem: .flexibleSpace)
        let dismiss = UIBarButtonItem(
            image: UIImage(systemName: "keyboard.chevron.compact.down"),
            style: .plain,
            target: context.coordinator,
            action: #selector(Coordinator.dismissKeyboard)
        )
        dismiss.accessibilityLabel = "收起键盘"
        toolbar.items = [flexible, dismiss]
        editor.inputAccessoryView = toolbar
        context.coordinator.editor = editor
        updateUIView(editor, context: context)
        return editor
    }

    func updateUIView(_ editor: IOSComposerTextView, context: Context) {
        context.coordinator.parent = self
        if editor.text != text, editor.markedTextRange == nil {
            let selection = editor.selectedRange
            editor.text = text
            let length = (text as NSString).length
            editor.selectedRange = NSRange(location: min(selection.location, length), length: 0)
        }
        editor.placeholder = placeholder
        editor.isEditable = isEditable
        editor.isSelectable = true
        editor.invalidateIntrinsicContentSize()
        DispatchQueue.main.async { [weak editor, weak coordinator = context.coordinator] in
            guard let editor, let coordinator else { return }
            coordinator.updateHeight(editor)
        }

        if context.coordinator.lastFocusRequest != focusRequest {
            context.coordinator.lastFocusRequest = focusRequest
            DispatchQueue.main.async { [weak editor] in
                guard let editor, editor.isEditable else { return }
                editor.becomeFirstResponder()
            }
        }
        if context.coordinator.lastDismissRequest != dismissRequest {
            context.coordinator.lastDismissRequest = dismissRequest
            DispatchQueue.main.async { [weak editor] in editor?.resignFirstResponder() }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    @MainActor final class Coordinator: NSObject, UITextViewDelegate {
        var parent: IOSMessageEditor
        weak var editor: UITextView?
        var lastFocusRequest = 0
        var lastDismissRequest = 0

        init(_ parent: IOSMessageEditor) { self.parent = parent }

        func textViewDidChange(_ textView: UITextView) {
            parent.text = textView.text
            textView.setNeedsDisplay()
            textView.invalidateIntrinsicContentSize()
            updateHeight(textView)
        }

        func updateHeight(_ textView: UITextView) {
            guard textView.bounds.width > 0 else { return }
            let natural = textView.sizeThatFits(
                CGSize(width: textView.bounds.width, height: .greatestFiniteMagnitude)
            ).height
            let height = min(max(natural, parent.minimumHeight), parent.maximumHeight)
            textView.isScrollEnabled = natural > parent.maximumHeight
            if abs(parent.measuredHeight - height) > 0.5 { parent.measuredHeight = height }
        }

        @objc func dismissKeyboard() { editor?.resignFirstResponder() }
    }
}
#endif
