import Foundation
import Testing
import YoloAgentsSDK
@testable import YoloAgentsUI

private actor AccountStateHTTP: HTTPTransport {
    private var responses: [HTTPResult]
    private(set) var requests: [URLRequest] = []
    init(_ bodies: [String], lastStatus: Int = 200) {
        responses = bodies.enumerated().map { HTTPResult(data: Data($0.element.utf8), status: $0.offset == bodies.count - 1 ? lastStatus : 200) }
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        guard !responses.isEmpty else { throw URLError(.badServerResponse) }
        if responses.count == 1 { return responses[0] }
        return responses.removeFirst()
    }
}
private let signedIn = "{\"account\":{\"status\":\"authenticated\",\"email\":\"fixture@example.test\",\"plan_type\":\"plus\"}}"
private let signedOut = "{\"account\":{\"status\":\"login_required\",\"email\":null,\"plan_type\":null}}"
private let pendingAccount = "{\"account\":{\"status\":\"login_pending\",\"email\":null,\"plan_type\":null}}"
private let loginStarted = "{\"login_id\":\"fixture-login\",\"auth_url\":\"https://chatgpt.com/oauth/authorize?state=fixture\"}"
private func accountConnection(platform: DevicePlatform = .macos) throws -> PairedConnection {
    let permissions = platform == .macos ? ["chat:read", "chat:write", "devices:manage"] : ["chat:read", "chat:write"]
    let body: [String: Any] = [
        "device": ["id": UUID().uuidString, "name": "Fixture", "platform": platform.rawValue,
                   "permissions": permissions, "created_at": "2026-10-27T00:00:00Z", "revoked_at": NSNull()],
        "token": "fixture-device-token", "server_id": UUID().uuidString, "api_version": 1,
    ]
    let pairing = try WireJSON.decoder().decode(PairingResponse.self, from: JSONSerialization.data(withJSONObject: body))
    return PairedConnection(endpoint: try ServiceEndpoint("https://example.com"), pairing: pairing)
}
@MainActor private func accountModel(_ transport: AccountStateHTTP, platform: DevicePlatform = .macos,
                                     timeout: Duration = .seconds(2)) throws -> CodexAccountModel {
    let connection = try accountConnection(platform: platform)
    return CodexAccountModel(connection: connection,
        client: APIClient(endpoint: connection.endpoint, token: connection.pairing.token, transport: transport),
        platform: platform, pollInterval: .milliseconds(10), loginTimeout: timeout)
}
@MainActor private func accountWait(_ condition: () async -> Bool) async throws {
    let deadline = ContinuousClock.now.advanced(by: .seconds(2))
    while !(await condition()) {
        if ContinuousClock.now > deadline { throw URLError(.timedOut) }
        try await Task.sleep(for: .milliseconds(5))
    }
}

@Test @MainActor func macAccountDisplaysManagedEmailAndPlan() async throws {
    let http = AccountStateHTTP([signedIn])
    let model = try accountModel(http)
    defer { model.stopWaiting() }
    await model.refresh()
    #expect(model.statusLabel == "已登录" && model.account?.email == "fixture@example.test")
    #expect(model.account?.planType == "plus" && model.canManage)
    #expect(await http.requests.first?.url?.path == "/v1/codex/account")
}

@Test @MainActor func iosUsesRedactedCatalogAndNeverCallsAccountManagement() async throws {
    let http = AccountStateHTTP(["{\"models\":[],\"default_model\":null,\"auth\":{\"status\":\"authenticated\",\"method\":\"chatgpt\"}}"])
    let model = try accountModel(http, platform: .ios)
    await model.refresh()
    #expect(model.statusLabel == "已登录" && !model.canManage)
    #expect(model.account?.email == nil && model.account?.planType == nil)
    #expect(await model.beginLogin() == nil)
    await model.logout()
    let requests = await http.requests
    #expect(requests.count == 1 && requests[0].url?.path == "/v1/models")
}

@Test @MainActor func browserLoginPollsUntilAuthenticatedWithoutOpeningAnyBrowserInTest() async throws {
    let http = AccountStateHTTP([loginStarted, pendingAccount, signedIn])
    let model = try accountModel(http)
    defer { model.stopWaiting() }
    let url = await model.beginLogin()
    #expect(url?.host == "chatgpt.com" && model.isWaitingForLogin)
    model.browserOpened(true)
    try await accountWait { !model.isWaitingForLogin }
    #expect(model.account?.isAuthenticated == true && model.errorMessage == nil)
    let paths = await http.requests.map { $0.url?.path }
    #expect(paths == ["/v1/codex/account/login", "/v1/codex/account", "/v1/codex/account"])
}

@Test @MainActor func stoppingBrowserWaitCancelsPollingAndDoesNotLogOut() async throws {
    let http = AccountStateHTTP([loginStarted, pendingAccount])
    let model = try accountModel(http)
    _ = await model.beginLogin()
    model.browserOpened(true)
    try await accountWait { await http.requests.count >= 2 }
    model.stopWaiting()
    let count = await http.requests.count
    try await Task.sleep(for: .milliseconds(35))
    let remainingCount = await http.requests.count
    #expect(!model.isWaitingForLogin && remainingCount == count)
    #expect(await http.requests.allSatisfy { $0.url?.path != "/v1/codex/account/logout" })
}

@Test @MainActor func invalidOrRejectedBrowserDoesNotStartAccountPolling() async throws {
    let http = AccountStateHTTP(["{\"auth_url\":\"https://evil.example/login\"}"])
    let model = try accountModel(http)
    #expect(await model.beginLogin() == nil)
    #expect(!model.isWaitingForLogin && model.errorMessage != nil)
    #expect(await http.requests.count == 1)
    let goodHTTP = AccountStateHTTP([loginStarted])
    let other = try accountModel(goodHTTP)
    _ = await other.beginLogin()
    other.browserOpened(false)
    #expect(!other.isWaitingForLogin && other.errorMessage?.contains("无法打开浏览器") == true)
    #expect(await goodHTTP.requests.count == 1)
}

@Test @MainActor func logoutRequiresManagementAndPreservesAccountWhenServerRejects() async throws {
    let http = AccountStateHTTP([signedIn, "{\"error\":{\"code\":\"codex_busy\",\"message\":\"请等待当前回复完成\"}}"], lastStatus: 409)
    let model = try accountModel(http)
    await model.refresh()
    await model.logout()
    #expect(model.account?.isAuthenticated == true)
    #expect(model.errorMessage?.contains("请等待当前回复完成") == true)
    #expect(await http.requests.last?.url?.path == "/v1/codex/account/logout")
    let connection = try accountConnection()
    #expect(!AccountAccess.canManage(platform: .ios, device: connection.pairing.device, serverAllows: true))
    #expect(!AccountAccess.canManage(platform: .macos, device: connection.pairing.device, serverAllows: false))
}

@Test @MainActor func successfulLogoutShowsLoggedOutAndWaitingHasBoundedTimeout() async throws {
    let http = AccountStateHTTP([signedIn, signedOut])
    let model = try accountModel(http)
    await model.refresh(); await model.logout()
    #expect(model.statusLabel == "未登录" && model.account?.email == nil)
    let pendingHTTP = AccountStateHTTP([loginStarted, pendingAccount])
    let pending = try accountModel(pendingHTTP, timeout: .milliseconds(25))
    defer { pending.stopWaiting() }
    _ = await pending.beginLogin(); pending.browserOpened(true)
    try await accountWait { !pending.isWaitingForLogin }
    #expect(pending.errorMessage?.contains("超时") == true)
}
