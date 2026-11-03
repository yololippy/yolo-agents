import Foundation
import Testing
@testable import YoloAgentsUI
#if os(macOS)
import AppKit
#endif

@Test func markdownLinksAllowOnlyHTTPS() throws {
    #expect(AgentMarkdownLinkPolicy.allows(try #require(URL(string: "https://example.com/path"))))
    #expect(!AgentMarkdownLinkPolicy.allows(try #require(URL(string: "http://example.com"))))
    #expect(!AgentMarkdownLinkPolicy.allows(try #require(URL(string: "file:///tmp/private"))))
    #expect(!AgentMarkdownLinkPolicy.allows(try #require(URL(string: "javascript:alert(1)"))))
}

@Test func markdownStreamEmitsCompleteGrowingSnapshotsAndFinishes() async {
    let source = AgentMarkdownStream(initial: "**正在")
    var iterator = source.text.makeAsyncIterator()

    #expect(await iterator.next() == "**正在")
    source.update("**正在输出**")
    source.update("**正在输出**")
    source.finish()

    #expect(await iterator.next() == "**正在输出**")
    #expect(await iterator.next() == nil)
}

#if os(macOS)
@Test func macMarkdownUsesSystemBodyTypography() {
    let config = AgentMarkdownTheme.automatic.renderConfig(streaming: false)
    let bodySize = NSFont.preferredFont(forTextStyle: .body).pointSize

    #expect(abs(config.paragraphStyle.textFonts.normal.pointSize - bodySize) < 0.01)
    #expect(abs(config.orderedListStyle.textFonts.normal.pointSize - bodySize) < 0.01)
    #expect(abs(config.inlineStyle.linkTextFont.pointSize - bodySize) < 0.01)
    #expect(config.tableStyle.textFonts.normal.pointSize <= bodySize)
}
#endif
