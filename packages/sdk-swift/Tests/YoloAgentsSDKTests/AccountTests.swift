import Foundation
import Testing
@testable import YoloAgentsSDK

private actor AccountHTTP: HTTPTransport {
    private var responses: [String]
    private(set) var requests: [URLRequest] = []
    init(_ responses: [String]) { self.responses = responses }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        guard !responses.isEmpty else { throw URLError(.badServerResponse) }
        return HTTPResult(data: Data(responses.removeFirst().utf8), status: 200)
    }
}

@Test func managedAccountEnvelopeAndDetailsDecode() throws {
    let status = try WireJSON.decoder().decode(CodexAccountStatus.self, from: Data("""
    {"account":{"status":"authenticated","email":"fixture@example.test","plan_type":"plus"}}
    """.utf8))
    #expect(status.isAuthenticated)
    #expect(status.email == "fixture@example.test")
    #expect(status.planType == "plus")
    let pending = try WireJSON.decoder().decode(CodexAccountStatus.self, from: Data("{\"account\":{\"status\":\"login_pending\",\"email\":null,\"plan_type\":null}}".utf8))
    #expect(!pending.isAuthenticated)
    #expect(pending.status == "login_pending")
}

@Test func accountEndpointsUseActualCodexPathsBearerAndEmptyJSON() async throws {
    let transport = AccountHTTP([
        "{\"account\":{\"status\":\"login_required\",\"email\":null,\"plan_type\":null}}",
        "{\"login_id\":\"fixture-id\",\"auth_url\":\"https://chatgpt.com/oauth/authorize?state=fixture\"}",
        "{\"account\":{\"status\":\"login_required\",\"email\":null,\"plan_type\":null}}",
    ])
    let client = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture-device-token", transport: transport)
    _ = try await client.account()
    let login = try await client.startAccountLogin()
    _ = try await client.logoutAccount()
    #expect(try login.authorizationURL()?.host == "chatgpt.com")
    let requests = await transport.requests
    #expect(requests.map(\.httpMethod) == ["GET", "POST", "POST"])
    #expect(requests.map { $0.url?.path } == ["/v1/codex/account", "/v1/codex/account/login", "/v1/codex/account/logout"])
    #expect(requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-device-token" })
    #expect(requests[1].httpBody == Data("{}".utf8) && requests[2].httpBody == Data("{}".utf8))
}

@Test(arguments: ["https://chatgpt.com/oauth/authorize?state=fixture", "https://auth.openai.com/oauth/authorize?state=fixture"])
func loginAllowsOnlyCurrentOfficialAuthorizationHosts(_ address: String) throws {
    let object = ["auth_url": address]
    let data = try JSONSerialization.data(withJSONObject: object)
    let response = try WireJSON.decoder().decode(CodexLoginResponse.self, from: data)
    #expect(try response.authorizationURL() != nil)
}

@Test(arguments: [
    "http://chatgpt.com/login", "https://auth0.openai.com/login", "https://chatgpt.com.evil.example/login",
    "https://auth.openai.com@evil.example/login", "https://user:pass@chatgpt.com/login", "https://chatgpt.com:8443/login",
    "https://chatgpt.com/login#token", "https://chatgpt.com/login?access_token=fixture", "https://chatgpt.com/login?API_KEY=fixture",
    " https://chatgpt.com/login", "https://chatgpt.com/lo gin", "https://chatgpt.com\\evil.example/login",
])
func unsafeAuthorizationURLIsRejected(_ address: String) throws {
    let data = try JSONSerialization.data(withJSONObject: ["auth_url": address])
    let response = try WireJSON.decoder().decode(CodexLoginResponse.self, from: data)
    #expect(throws: ClientError.untrustedAuthorizationURL) { try response.authorizationURL() }
}

@Test func missingAccountStateDoesNotPretendToBeLoggedOut() {
    #expect(throws: (any Error).self) {
        try WireJSON.decoder().decode(CodexAccountStatus.self, from: Data("{\"email\":null}".utf8))
    }
}
