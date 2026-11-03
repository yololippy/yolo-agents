import Foundation
import SwiftUI
import SwiftStreamingMarkdown

/// Agent-neutral Markdown colors. Apps can provide their own palette without
/// depending on the renderer's public API or duplicating its integration.
public struct AgentMarkdownTheme: Hashable, Sendable {
    public var textColor: Color
    public var secondaryTextColor: Color
    public var accentColor: Color
    public var codeBackgroundColor: Color

    public init(
        textColor: Color = .primary,
        secondaryTextColor: Color = .secondary,
        accentColor: Color = .accentColor,
        codeBackgroundColor: Color = Color.secondary.opacity(0.12)
    ) {
        self.textColor = textColor
        self.secondaryTextColor = secondaryTextColor
        self.accentColor = accentColor
        self.codeBackgroundColor = codeBackgroundColor
    }

    public static let automatic = AgentMarkdownTheme()

    fileprivate func renderConfig(streaming: Bool) -> MarkdownRenderConfig {
        let defaults = MarkdownRenderConfig.default
        return defaults
            .withShouldAnimateText(value: streaming)
            .withBlockSpacing(value: 12)
            .withImageConfig(.disabled)
            .withParagraphStyle(value: .init(
                textFonts: defaults.paragraphStyle.textFonts,
                textColor: textColor
            ))
            .withBlockQuoteStyle(value: .init(
                textFonts: defaults.blockQuoteStyle.textFonts,
                textColor: secondaryTextColor
            ))
            .withOrderedListStyle(value: .init(
                textFonts: defaults.orderedListStyle.textFonts,
                textColor: textColor
            ))
            .withHeadingStyle(value: .init(
                h1Font: defaults.headingStyle.h1Font,
                h2Font: defaults.headingStyle.h2Font,
                h3Font: defaults.headingStyle.h3Font,
                h4Font: defaults.headingStyle.h4Font,
                h5Font: defaults.headingStyle.h5Font,
                h6Font: defaults.headingStyle.h6Font,
                textColor: textColor
            ))
            .withInlineStyle(value: .init(
                boldTextColor: textColor,
                linkTextFont: defaults.inlineStyle.linkTextFont,
                linkTextColor: accentColor,
                linkUnderlineStyle: defaults.inlineStyle.linkUnderlineStyle,
                codeTextFont: defaults.inlineStyle.codeTextFont,
                codeTextColor: textColor,
                codeBackgroundColor: codeBackgroundColor,
                codeUnderlineColor: accentColor.opacity(0.55)
            ))
    }
}

enum AgentMarkdownLinkPolicy {
    static func allows(_ url: URL) -> Bool {
        url.scheme?.lowercased() == "https"
    }

    @MainActor static let openURL = OpenURLAction { url in
        allows(url) ? .systemAction(url) : .discarded
    }
}

/// Renders a completed assistant message. User-authored prompts intentionally
/// stay as plain text so their exact source is never reinterpreted.
public struct AgentMarkdownView: View {
    private let markdown: String
    private let theme: AgentMarkdownTheme

    public init(_ markdown: String, theme: AgentMarkdownTheme = .automatic) {
        self.markdown = markdown
        self.theme = theme
    }

    public var body: some View {
        MarkdownView(text: markdown, config: theme.renderConfig(streaming: false))
            .environment(\.openURL, AgentMarkdownLinkPolicy.openURL)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Renders the growing snapshot of one assistant reply. The source has a
/// stable lifetime so incremental updates do not replace the renderer.
public struct AgentStreamingMarkdownView: View {
    private let markdown: String
    private let theme: AgentMarkdownTheme
    @State private var source: AgentMarkdownStream

    public init(_ markdown: String, theme: AgentMarkdownTheme = .automatic) {
        self.markdown = markdown
        self.theme = theme
        _source = State(initialValue: AgentMarkdownStream(initial: markdown))
    }

    public var body: some View {
        StreamedMarkdownView(source: source, config: theme.renderConfig(streaming: true))
            .environment(\.openURL, AgentMarkdownLinkPolicy.openURL)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .onChange(of: markdown, initial: true) { _, next in
                source.update(next)
            }
    }
}

final class AgentMarkdownStream: StreamedMarkdownSource, @unchecked Sendable {
    let text: AsyncStream<String>
    private let continuation: AsyncStream<String>.Continuation
    private let lock = NSLock()
    private var latest: String
    private var finished = false

    init(initial: String) {
        let pair = Self.makeStream()
        text = pair.stream
        continuation = pair.continuation
        latest = initial
        continuation.yield(initial)
    }

    func update(_ next: String) {
        lock.lock()
        guard !finished, next != latest else {
            lock.unlock()
            return
        }
        latest = next
        lock.unlock()
        continuation.yield(next)
    }

    func finish() {
        lock.lock()
        guard !finished else {
            lock.unlock()
            return
        }
        finished = true
        lock.unlock()
        continuation.finish()
    }

    deinit {
        continuation.finish()
    }

    private static func makeStream() -> (
        stream: AsyncStream<String>, continuation: AsyncStream<String>.Continuation
    ) {
        var stored: AsyncStream<String>.Continuation?
        let stream = AsyncStream<String>(bufferingPolicy: .bufferingNewest(1)) { continuation in
            stored = continuation
        }
        return (stream, stored!)
    }
}
