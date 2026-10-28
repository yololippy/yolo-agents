import Foundation
import Testing
import YoloAgentsSDK
@testable import YoloAgentsUI

private let accountServerID = "40000000-0000-4000-8000-000000000004"
private let accountIdentity = AgentAppIdentity(agentID: "b-agent", appName: "B Agent", keychainService: "fixture.b", defaultServiceAddress: "http://127.0.0.1:8766")
private let legacyIdentity = AgentAppIdentity(agentID: "trading-agent", appName: "Trading Agent", keychainService: "fixture.trading", defaultServiceAddress: "http://127.0.0.1:8765", allowsLegacyServer: true)
private actor AccountStateHTTP: HTTPTransport {
    private(set) var requests: [URLRequest] = []
    private var status: String
    private var authenticatedAfter: Int?
    private var pendingReads = 0
    private var accountAgent = "b-agent"
    private var healthAgent: String? = "b-agent"
    private var failure: (String, Int)?
    private var officialURL = "https://auth.openai.com/oauth/authorize?state=fixture"
    private var legacy = false
    init(status: String = "login_required", authenticatedAfter: Int? = nil) {
        self.status = status; self.authenticatedAfter = authenticatedAfter
    }
    func legacyOnly() { legacy = true; healthAgent = nil; accountAgent = "trading-agent" }
    func setHealthAgent(_ value: String?) { healthAgent = value }
    func setAccountAgent(_ value: String) { accountAgent = value }
    func fail(_ path: String, status: Int) { failure = (path, status) }
    func clearFailure() { failure = nil }
    func unsafeURL() { officialURL = "https://evil.example/login" }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        let path = request.url!.path
        if let (target, status) = failure, target == path {
            return try response(["error": ["code": "fixture_error", "message": "Fixture rejected"]], status: status)
        }
        if path == "/v1/health" {
            var body: [String: Any] = ["status": "ok", "api_version": 1, "server_id": accountServerID, "agent_mode": "chatgpt", "trade_execution_enabled": false]
            body["agent_id"] = healthAgent
            return try response(body)
        }
        if path == "/v1/models" {
            return try response(["models": [], "auth": ["status": status, "email": "must-not-copy@example.test", "plan_type": "secret"]])
        }
        if legacy && path == "/v1/account" { return try response(["error": ["code": "not_found", "message": "Legacy"]], status: 404) }
        if path.hasSuffix("/login/cancel") {
            status = "login_required"
            return try response(["account": account()])
        }
        if path.hasSuffix("/login") {
            status = "login_pending"
            return try response(["login_id": "fixture-login", "auth_url": officialURL])
        }
        if path.hasSuffix("/logout") {
            status = "login_required"
            var result = account(); result["error"] = ["code": "remote_revocation_unconfirmed"]
            return try response(["account": result])
        }
        if status == "login_pending" {
            pendingReads += 1
            if let authenticatedAfter, pendingReads >= authenticatedAfter { status = "authenticated" }
        }
        return try response(["account": account()])
    }
    private func account() -> [String: Any] {
        var value: [String: Any] = ["status": status, "method": legacy ? "codex" : "chatgpt", "can_manage": true]
        if !legacy { value["agent_id"] = accountAgent; value["app_name"] = "B Agent" }
        if status == "authenticated" { value["email"] = "fixture@example.test"; value["plan_type"] = "plus" }
        return value
    }
    private func response(_ value: [String: Any], status: Int = 200) throws -> HTTPResult {
        HTTPResult(data: try JSONSerialization.data(withJSONObject: value), status: status)
    }
}
private func accountConnection(platform: DevicePlatform, identity: AgentAppIdentity) throws -> PairedConnection {
    let body: [String: Any] = [
        "device": ["id": UUID().uuidString, "name": "Fixture", "platform": platform.rawValue,
                   "permissions": platform == .macos ? ["chat:read", "chat:write", "devices:manage"] : ["chat:read", "chat:write"],
                   "created_at": "2026-10-28T00:00:00Z"], "token": "fixture-device-token", "server_id": accountServerID, "api_version": 1,
    ]
    let pairing = try WireJSON.decoder().decode(PairingResponse.self, from: JSONSerialization.data(withJSONObject: body))
    return PairedConnection(endpoint: try ServiceEndpoint("https://example.test"), pairing: pairing,
                            agentID: identity.allowsLegacyServer ? nil : identity.agentID)
}
@MainActor private func accountModel(_ http: AccountStateHTTP, platform: DevicePlatform = .macos,
    identity: AgentAppIdentity = accountIdentity, timeout: Duration = .seconds(2)) throws -> ChatGPTAccountModel {
    let connection = try accountConnection(platform: platform, identity: identity)
    return ChatGPTAccountModel(connection: connection, identity: identity,
        client: APIClient(endpoint: connection.endpoint, token: connection.pairing.token, transport: http),
        platform: platform, pollInterval: .milliseconds(10), loginTimeout: timeout)
}
@MainActor private func accountWait(_ condition: () async -> Bool) async throws {
    let deadline = ContinuousClock.now.advanced(by: .seconds(2))
    while !(await condition()) {
        if ContinuousClock.now > deadline { throw URLError(.timedOut) }
        try await Task.sleep(for: .milliseconds(5))
    }
}

@Test @MainActor func providerAccountDisplaysOwningAppAndIndependentLogout() async throws {
    let http = AccountStateHTTP(status: "authenticated")
    let model = try accountModel(http)
    await model.refresh()
    #expect(model.appName == "B Agent" && model.canManage && !model.usesCodex)
    #expect(model.account?.email == "fixture@example.test" && model.account?.planType == "plus")
    #expect(model.logoutExplanation.contains("仅退出 B Agent") && !model.logoutExplanation.contains("Codex"))
    await model.logout()
    #expect(model.statusLabel == "未登录" && model.noticeMessage?.contains("远程授权撤销尚未确认") == true)
    #expect(model.errorMessage?.contains("远程授权撤销尚未确认") == true)
    #expect(await http.requests.last?.url?.path == "/v1/account/logout")
}

@Test @MainActor func iosUsesOnlyRedactedCatalogAndNeverCallsAccountManagement() async throws {
    let http = AccountStateHTTP(status: "authenticated")
    let model = try accountModel(http, platform: .ios)
    await model.refresh()
    #expect(model.statusLabel == "已登录" && !model.canManage && model.account?.email == nil && model.account?.planType == nil)
    #expect(await model.beginLogin() == nil)
    await model.logout(); await model.cancelLogin()
    #expect(await http.requests.map { $0.url?.path } == ["/v1/health", "/v1/models"])
}

@Test @MainActor func browserLoginPollsUntilAuthenticatedWithoutOpeningAnyBrowserInTest() async throws {
    let http = AccountStateHTTP(authenticatedAfter: 2)
    let model = try accountModel(http)
    defer { model.stopWaiting() }
    let url = await model.beginLogin()
    #expect(url?.host == "auth.openai.com" && model.isWaitingForLogin)
    model.browserOpened(true)
    try await accountWait { !model.isWaitingForLogin }
    #expect(model.account?.isAuthenticated == true && model.errorMessage == nil)
    #expect(await http.requests.filter { $0.httpMethod == "POST" }.map { $0.url?.path } == ["/v1/account/login"])
}

@Test @MainActor func cancellingLoginCancelsBackendAndStopsPolling() async throws {
    let http = AccountStateHTTP()
    let model = try accountModel(http)
    _ = await model.beginLogin(); model.browserOpened(true)
    try await accountWait { await http.requests.count >= 5 }
    await model.cancelLogin()
    #expect(!model.isWaitingForLogin && model.noticeMessage?.contains("已取消") == true)
    let count = await http.requests.count
    try await Task.sleep(for: .milliseconds(30))
    #expect(await http.requests.count == count)
    #expect(await http.requests.last?.url?.path == "/v1/account/login/cancel")
    #expect(await http.requests.last?.httpBody == Data("{}".utf8))
}

@Test @MainActor func cancellationFailureDoesNotPretendLoginWasCancelled() async throws {
    let http = AccountStateHTTP()
    let model = try accountModel(http)
    _ = await model.beginLogin()
    await http.fail("/v1/account/login/cancel", status: 503)
    await model.cancelLogin()
    #expect(model.isWaitingForLogin && model.errorMessage?.contains("尚未确认") == true)
    #expect(await model.beginLogin() == nil)
    await http.clearFailure()
    await model.cancelLogin()
    #expect(!model.isWaitingForLogin)
}

@Test @MainActor func rejectedBrowserAndUnsafeURLCancelBackendLogin() async throws {
    let http = AccountStateHTTP()
    let model = try accountModel(http)
    _ = await model.beginLogin(); model.browserOpened(false)
    try await accountWait { !model.isWaitingForLogin }
    #expect(model.errorMessage?.contains("无法打开浏览器") == true)
    #expect(await http.requests.last?.url?.path == "/v1/account/login/cancel")
    let unsafe = AccountStateHTTP()
    await unsafe.unsafeURL()
    let other = try accountModel(unsafe)
    #expect(await other.beginLogin() == nil)
    #expect(!other.isWaitingForLogin && other.errorMessage != nil)
    #expect(await unsafe.requests.last?.url?.path == "/v1/account/login/cancel")
}

@Test @MainActor func logoutFailurePreservesAccountAndLoginTimeoutCancelsBackend() async throws {
    let http = AccountStateHTTP(status: "authenticated")
    let model = try accountModel(http)
    await model.refresh(); await http.fail("/v1/account/logout", status: 409)
    await model.logout()
    #expect(model.account?.isAuthenticated == true && model.errorMessage != nil)
    let pendingHTTP = AccountStateHTTP()
    let pending = try accountModel(pendingHTTP, timeout: .milliseconds(25))
    defer { pending.stopWaiting() }
    _ = await pending.beginLogin(); pending.browserOpened(true)
    try await accountWait { !pending.isWaitingForLogin }
    #expect(pending.errorMessage?.contains("超时") == true)
    #expect(await pendingHTTP.requests.last?.url?.path == "/v1/account/login/cancel")
}

@Test @MainActor func legacyTradingFallbackIsReadDiscoveryOnlyAndShowsRealCodexImpact() async throws {
    let http = AccountStateHTTP(status: "authenticated")
    await http.legacyOnly()
    let model = try accountModel(http, identity: legacyIdentity)
    await model.refresh()
    #expect(model.canManage && model.usesCodex && model.logoutExplanation.contains("Codex"))
    #expect(await http.requests.map { $0.url?.path } == ["/v1/health", "/v1/account", "/v1/codex/account"])
    await model.logout()
    #expect(await http.requests.last?.url?.path == "/v1/codex/account/logout")
    let failed = AccountStateHTTP()
    await failed.fail("/v1/account/login", status: 503)
    let other = try accountModel(failed)
    #expect(await other.beginLogin() == nil)
    #expect(await failed.requests.allSatisfy { $0.url?.path.contains("/codex/") == false })
}

@Test @MainActor func wrongAgentAndUnidentifiedHealthBlockPrivateAccountCalls() async throws {
    for id in ["trading-agent", nil] {
        let http = AccountStateHTTP()
        await http.setHealthAgent(id)
        let model = try accountModel(http)
        await model.refresh()
        #expect(model.errorMessage != nil && !model.canManage)
        #expect(await model.beginLogin() == nil)
        #expect(await http.requests.allSatisfy { $0.url?.path == "/v1/health" && $0.value(forHTTPHeaderField: "Authorization") == nil })
    }
    let mismatched = AccountStateHTTP()
    await mismatched.setAccountAgent("trading-agent")
    let other = try accountModel(mismatched)
    await other.refresh()
    #expect(other.account == nil && !other.canManage && other.errorMessage != nil)
}

@Test(arguments: ["expired", "permission_required"]) @MainActor
func credentialsCanBeClearedWhenRefreshOrPlanConsentIsNeeded(_ status: String) async throws {
    let http = AccountStateHTTP(status: status)
    let model = try accountModel(http)
    await model.refresh()
    #expect(model.canLogout && model.appName == "B Agent" && model.account?.method == "chatgpt")
    if status == "expired" { #expect(model.statusLabel == "登录状态需要刷新") }
    await model.logout()
    #expect(model.statusLabel == "未登录" && !model.canLogout)
    #expect(await http.requests.last?.url?.path == "/v1/account/logout")
}
