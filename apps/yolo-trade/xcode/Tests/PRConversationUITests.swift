import XCTest
import CryptoKit
#if os(macOS)
import AppKit
#else
import UIKit
#endif

/// The only message-producing operations in this gate are editor input and the
/// production send button. No HTTP message API, SDK probe or injected reply is used.
@MainActor
final class PRConversationUITests: XCTestCase {
    enum Failure: Error {
        case configuration, missingConversation, ambiguousConversation, missingEditor
        case missingConversationMenu, missingSendControl, missingCleanupAcknowledgment
        case missingReply, conversationIdentity
    }
    private var app: XCUIApplication?

    override func setUpWithError() throws { continueAfterFailure = false }
    override func tearDownWithError() throws {
        guard let app else { return }
        if (testRun?.totalFailureCount ?? 0) > 0 {
            // Private xcresult diagnostics only; never print the hierarchy or
            // include it in the sanitized evidence exported to the PR report.
            let hierarchy = XCTAttachment(string: app.debugDescription)
            hierarchy.name = "pr-ui-hierarchy.txt"
            hierarchy.lifetime = .keepAlways
            add(hierarchy)
        }
        app.terminate()
    }

    func testConversationRoundTripAndSync() throws {
        let env = ProcessInfo.processInfo.environment
        let prefix = "YOLO_TRADE_PR_UI_"
        guard let runID = env[prefix + "RUN_ID"],
              runID.range(of: "^[a-f0-9]{12}4[a-f0-9]{3}[89ab][a-f0-9]{15}$", options: .regularExpression) != nil,
              let phase = env[prefix + "PHASE"],
              let endpoint = env[prefix + "ENDPOINT"],
              let code = env[prefix + "PAIRING_CODE"], !code.isEmpty,
              let bundle = env[prefix + "BUNDLE_ID"],
              bundle == "com.yoloagents.yolotrade.prtest.\(runID)" else {
            throw Failure.configuration
        }
        #if os(macOS)
        let platform = "macos"
        guard ["mac_send", "mac_verify", "mac_cleanup"].contains(phase) else { throw Failure.configuration }
        #else
        let platform = "ios"
        guard ["ios_sync_send", "ios_cleanup"].contains(phase) else { throw Failure.configuration }
        #endif
        let app = XCUIApplication(bundleIdentifier: bundle)
        self.app = app
        app.launchEnvironment = [prefix + "RUN_ID": runID, prefix + "ENDPOINT": endpoint,
                                 prefix + "PAIRING_CODE": code, prefix + "PLATFORM": platform,
                                 prefix + "CLEANUP": phase.hasSuffix("cleanup") ? "1" : "0"]
        app.launch()
        if phase.hasSuffix("cleanup") {
            guard app.staticTexts["prtest.cleanup.complete"].waitForExistence(timeout: 30) else {
                throw Failure.missingCleanupAcknowledgment
            }
            emit(runID: runID, phase: phase, conversationHash: nil, flags: ["cleanup_complete": true])
            return
        }
        let conversationTitle = "PR UI " + runID
        // Select the dedicated temporary conversation through the ordinary sidebar/list.
        #if os(macOS)
        let conversation = try macConversationRow(app: app, title: conversationTitle, timeout: 60)
        #else
        let conversation = app.staticTexts[conversationTitle].firstMatch
        guard conversation.waitForExistence(timeout: 60) else { throw Failure.missingConversation }
        #endif
        activate(conversation)
        let editor = app.textViews["chat.composer"].firstMatch
        guard editor.waitForExistence(timeout: 60) else { throw Failure.missingEditor }
        let conversationHash = try copyConversationHash(app: app, title: conversationTitle)
        let macToken = "MACUI" + runID
        let iosToken = "IOSUI" + runID
        let macPrompt = prompt(macToken)
        let iosPrompt = prompt(iosToken)
        var flags: [String: Bool] = [:]
        switch phase {
        case "mac_send":
            try send(macPrompt, editor: editor, app: app)
            try waitForText(macPrompt, app: app)
            try waitForReply(macToken, app: app)
            flags = ["mac_prompt_seen": true, "mac_reply_seen": true]
        case "ios_sync_send":
            try waitForText(macPrompt, app: app)
            try waitForReply(macToken, app: app)
            try send(iosPrompt, editor: editor, app: app)
            try waitForText(iosPrompt, app: app)
            try waitForReply(iosToken, app: app)
            flags = ["mac_prompt_seen": true, "mac_reply_seen": true,
                     "ios_prompt_seen": true, "ios_reply_seen": true]
        case "mac_verify":
            try waitForText(macPrompt, app: app)
            try waitForReply(macToken, app: app)
            try waitForText(iosPrompt, app: app)
            try waitForReply(iosToken, app: app)
            flags = ["mac_prompt_seen": true, "mac_reply_seen": true,
                     "ios_prompt_seen": true, "ios_reply_seen": true]
        default: throw Failure.configuration
        }
        emit(runID: runID, phase: phase, conversationHash: conversationHash, flags: flags)
    }

    private func prompt(_ token: String) -> String {
        "This is a harmless YoloTrade UI synchronization check. Reply with exactly \(token) and nothing else. Do not use any tools."
    }

    private func activate(_ element: XCUIElement) {
        #if os(macOS)
        element.click()
        #else
        element.tap()
        #endif
    }

    private func send(_ prompt: String, editor: XCUIElement, app: XCUIApplication) throws {
        activate(editor)
        editor.typeText(prompt)
        let button = app.buttons["chat.send"].firstMatch
        let ready = NSPredicate { _, _ in button.exists && button.isEnabled }
        guard XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: ready, object: nil)], timeout: 30) == .completed else {
            throw Failure.missingSendControl
        }
        activate(button)
    }

    private func waitForText(_ text: String, app: XCUIApplication) throws {
        guard app.staticTexts.matching(NSPredicate(format: "label == %@", text)).firstMatch
            .waitForExistence(timeout: 120) else { throw Failure.missingReply }
    }

    private func waitForReply(_ token: String, app: XCUIApplication) throws {
        let reply = app.staticTexts.matching(NSPredicate(format: "label MATCHES %@", "\\s*" + token + "\\s*")).firstMatch
        let complete = NSPredicate { _, _ in
            reply.exists && !app.staticTexts["正在回复…"].exists &&
                !app.descendants(matching: .any).matching(
                    NSPredicate(format: "identifier BEGINSWITH %@", "chat.awaiting-reply.")).firstMatch.exists
        }
        guard XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: complete, object: nil)], timeout: 180) == .completed else {
            throw Failure.missingReply
        }
    }

    #if os(macOS)
    private func macConversationRow(app: XCUIApplication, title: String, timeout: TimeInterval) throws -> XCUIElement {
        // SwiftUI exposes the combined title/date/badge on the row itself,
        // without a StaticText child. Match the full title (including run UUID)
        // on native rows/cells; a detail-pane StaticText is never a candidate.
        let label = NSPredicate(format: "label CONTAINS %@", title)
        let rowQueries = [
            app.outlineRows.matching(label),
            app.tableRows.matching(label),
            app.cells.matching(label),
        ]
        let available = NSPredicate { _, _ in rowQueries.contains { $0.count > 0 } }
        guard XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: available, object: nil)],
                            timeout: timeout) == .completed else {
            throw Failure.missingConversation
        }
        for rows in rowQueries where rows.count > 0 {
            guard rows.count == 1 else { throw Failure.ambiguousConversation }
            return rows.element(boundBy: 0)
        }
        throw Failure.missingConversation
    }
    #endif

    private func copyConversationHash(app: XCUIApplication, title: String) throws -> String {
        #if os(macOS)
        let conversation = try macConversationRow(app: app, title: title, timeout: 30)
        conversation.rightClick()
        let copy = app.menuItems["复制会话 ID"].firstMatch
        guard copy.waitForExistence(timeout: 10) else { throw Failure.missingConversationMenu }
        copy.click()
        let value = NSPasteboard.general.string(forType: .string)
        #else
        let menu = app.buttons["chat.conversation-menu"].firstMatch
        guard menu.waitForExistence(timeout: 30) else { throw Failure.missingConversationMenu }
        menu.tap()
        let copy = app.buttons["复制会话 ID"].firstMatch
        guard copy.waitForExistence(timeout: 10) else { throw Failure.missingConversationMenu }
        copy.tap()
        let value = UIPasteboard.general.string
        #endif
        guard let value, let id = UUID(uuidString: value) else { throw Failure.conversationIdentity }
        return SHA256.hash(data: Data(id.uuidString.lowercased().utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private func emit(runID: String, phase: String, conversationHash: String?, flags: [String: Bool]) {
        var evidence: [String: Any] = ["schema_version": 1, "run_id": runID, "phase": phase, "flags": flags]
        if let conversationHash { evidence["conversation_hash"] = conversationHash }
        // No content, identifiers, endpoint, pairing code or credential is emitted.
        let data = try! JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys])
        let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        attachment.name = "pr-ui-evidence.json"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
