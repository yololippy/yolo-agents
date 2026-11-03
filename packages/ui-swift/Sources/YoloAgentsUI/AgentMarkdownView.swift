import Foundation
import SwiftUI
import SwiftStreamingMarkdown
#if os(macOS)
import AppKit
#endif

#if os(macOS)
private enum AgentMarkdownTypography {
    private static var bodySize: CGFloat {
        NSFont.preferredFont(forTextStyle: .body).pointSize
    }

    static var body: TextFonts { fonts(size: bodySize, lineHeight: bodySize * 1.5) }
    static var small: TextFonts {
        let size = max(11, bodySize - 1)
        return fonts(size: size, lineHeight: size * 1.45)
    }
    static var code: TextFonts {
        let size = max(11, bodySize - 1)
        return fonts(size: size, lineHeight: size * 1.5, monospaced: true)
    }
    static var h1: TextFonts { fonts(size: bodySize + 8, lineHeight: bodySize + 13) }
    static var h2: TextFonts { fonts(size: bodySize + 6, lineHeight: bodySize + 11) }
    static var h3: TextFonts { fonts(size: bodySize + 4, lineHeight: bodySize + 9) }
    static var h4: TextFonts { fonts(size: bodySize + 3, lineHeight: bodySize + 8) }
    static var h5: TextFonts { fonts(size: bodySize + 2, lineHeight: bodySize + 7) }
    static var h6: TextFonts { fonts(size: bodySize + 1, lineHeight: bodySize + 6) }

    private static func fonts(size: CGFloat, lineHeight: CGFloat,
                              monospaced: Bool = false) -> TextFonts {
        func make(_ weight: NSFont.Weight, italic: Bool = false) -> NSFont {
            let base = monospaced
                ? NSFont.monospacedSystemFont(ofSize: size, weight: weight)
                : NSFont.systemFont(ofSize: size, weight: weight)
            guard italic else { return base }
            return NSFont(descriptor: base.fontDescriptor.withSymbolicTraits(.italic), size: size) ?? base
        }
        return TextFonts(
            normal: make(.regular), italic: make(.regular, italic: true),
            bold: make(.semibold), boldItalic: make(.semibold, italic: true),
            preferredLetterSpacing: 0, preferredLineHeight: lineHeight
        )
    }
}
#endif

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

    func renderConfig(streaming: Bool) -> MarkdownRenderConfig {
        let defaults = MarkdownRenderConfig.default
        #if os(macOS)
        let bodyFonts = AgentMarkdownTypography.body
        let smallFonts = AgentMarkdownTypography.small
        let codeFonts = AgentMarkdownTypography.code
        let h1Fonts = AgentMarkdownTypography.h1
        let h2Fonts = AgentMarkdownTypography.h2
        let h3Fonts = AgentMarkdownTypography.h3
        let h4Fonts = AgentMarkdownTypography.h4
        let h5Fonts = AgentMarkdownTypography.h5
        let h6Fonts = AgentMarkdownTypography.h6
        #else
        let bodyFonts = defaults.paragraphStyle.textFonts
        let smallFonts = defaults.tableStyle.textFonts
        let codeFonts = defaults.codeBlockConfig.codeTextFonts
        let h1Fonts = defaults.headingStyle.h1Font
        let h2Fonts = defaults.headingStyle.h2Font
        let h3Fonts = defaults.headingStyle.h3Font
        let h4Fonts = defaults.headingStyle.h4Font
        let h5Fonts = defaults.headingStyle.h5Font
        let h6Fonts = defaults.headingStyle.h6Font
        #endif
        return defaults
            .withShouldAnimateText(value: streaming)
            .withBlockSpacing(value: 12)
            .withImageConfig(.disabled)
            .withParagraphStyle(value: .init(
                textFonts: bodyFonts,
                textColor: textColor
            ))
            .withBlockQuoteStyle(value: .init(
                textFonts: bodyFonts,
                textColor: secondaryTextColor
            ))
            .withOrderedListStyle(value: .init(
                textFonts: bodyFonts,
                textColor: textColor
            ))
            .withTableStyle(value: .init(
                textFonts: smallFonts,
                headerTextColor: textColor,
                regularTextColor: textColor,
                headerBackgroundColor: defaults.tableStyle.headerBackgroundColor,
                borderColor: defaults.tableStyle.borderColor,
                actionButtonColor: accentColor
            ))
            .withHeadingStyle(value: .init(
                h1Font: h1Fonts,
                h2Font: h2Fonts,
                h3Font: h3Fonts,
                h4Font: h4Fonts,
                h5Font: h5Fonts,
                h6Font: h6Fonts,
                textColor: textColor
            ))
            .withInlineStyle(value: .init(
                boldTextColor: textColor,
                linkTextFont: bodyFonts.normal,
                linkTextColor: accentColor,
                linkUnderlineStyle: defaults.inlineStyle.linkUnderlineStyle,
                codeTextFont: codeFonts.normal,
                codeTextColor: textColor,
                codeBackgroundColor: codeBackgroundColor,
                codeUnderlineColor: accentColor.opacity(0.55)
            ))
            .withCodeBlockConfig(value: .init(
                theme: defaults.codeBlockConfig.theme,
                backgroundColor: defaults.codeBlockConfig.backgroundColor,
                foregroundColor: defaults.codeBlockConfig.foregroundColor,
                codeTextFonts: codeFonts,
                chromeTextFonts: smallFonts
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
