import Foundation
import Testing
@testable import YoloAgentsUI

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
