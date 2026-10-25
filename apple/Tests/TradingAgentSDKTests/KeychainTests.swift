#if os(iOS)
import Foundation
import Testing
import TradingAgentSDK

/// Uses only a unique test service/account. Never queries or removes production entries.
@Test func isolatedKeychainWriteReadUpdateDelete() throws {
    let store = KeychainStore(service: "com.tradingagent.sdk-tests.\(UUID().uuidString)")
    let account = "temporary-entry.\(UUID().uuidString)"
    defer {
        do { try store.remove(account) }
        catch { Issue.record("Could not remove the isolated Keychain test entry.") }
    }
    #expect(try store.read(account) == nil)
    let first = Data("temporary-first-value".utf8)
    let updated = Data("temporary-updated-value".utf8)
    try store.write(first, for: account)
    #expect(try store.read(account) == first)
    try store.write(updated, for: account)
    #expect(try store.read(account) == updated)
    try store.remove(account)
    #expect(try store.read(account) == nil)
    try store.remove(account)
}
#endif
