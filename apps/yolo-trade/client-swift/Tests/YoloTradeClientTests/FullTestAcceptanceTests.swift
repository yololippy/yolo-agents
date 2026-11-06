import Foundation
import Testing
import YoloAgentsSDK
@testable import YoloTradeClient

private actor AcceptanceHTTP: HTTPTransport {
    private(set) var requests: [URLRequest] = []
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        let object: Any
        switch request.url?.path {
        case "/v1/health":
            object = [
                "status": "ok", "api_version": 1,
                "server_id": "40000000-0000-4000-8000-000000000004",
                "agent_mode": "chatgpt", "agent_id": "yolo-trade",
                "trade_execution_enabled": true,
            ] as [String: Any]
        case "/v1/account":
            object = ["account": ["status": "authenticated", "agent_id": "yolo-trade"]]
        case "/v1/models":
            object = [
                "models": [["id": "fixture-model", "display_name": "Fixture"]],
                "default_model": "fixture-model", "auth": ["status": "authenticated"],
            ] as [String: Any]
        case "/v1/conversations":
            object = ["conversations": [[
                "id": "20000000-0000-4000-8000-000000000002", "title": "主会话",
                "created_at": "2026-11-06T00:00:00Z", "updated_at": "2026-11-06T00:00:00Z",
                "archived": false, "model": NSNull(),
            ]], "primary_conversation_id": "20000000-0000-4000-8000-000000000002"]
        case "/v1/admin/full-test/model-probe":
            object = ["probe": [
                "status": "passed", "model": "fixture-model",
                "business_tools_enabled": false, "conversation_saved": false,
            ]]
        case "/v1/admin/full-test/simulator-session":
            object = ["session": [
                "pairing_code": "fixture-pairing-code", "grant": "fixture-probe-grant",
                "expires_in": 300,
            ]]
        case "/v1/devices/pair":
            object = [
                "device": [
                    "id": "10000000-0000-4000-8000-000000000009",
                    "name": "YoloTrade Full Test Simulator", "platform": "ios",
                    "permissions": ["chat:read", "chat:write"],
                    "created_at": "2026-11-06T00:00:00Z", "revoked_at": NSNull(),
                ],
                "token": "fixture-ios-token",
                "server_id": "40000000-0000-4000-8000-000000000004", "api_version": 1,
            ] as [String: Any]
        case "/v1/full-test/model-probe":
            object = ["probe": [
                "status": "passed", "model": "fixture-model",
                "business_tools_enabled": false, "conversation_saved": false,
            ]]
        case "/v1/devices/10000000-0000-4000-8000-000000000009":
            object = ["device": [
                "id": "10000000-0000-4000-8000-000000000009",
                "name": "YoloTrade Full Test Simulator", "platform": "ios",
                "permissions": ["chat:read", "chat:write"],
                "created_at": "2026-11-06T00:00:00Z", "revoked_at": "2026-11-06T00:01:00Z",
            ]]
        default:
            throw URLError(.badURL)
        }
        return HTTPResult(
            data: try JSONSerialization.data(withJSONObject: object), status: 200
        )
    }
}

@Test func simulatorProbeUsesMacIssuedSessionAndRevokesTemporaryIOSIdentity() async throws {
    let http = AcceptanceHTTP()
    let session = try await YoloTradeFullTestAcceptance.issueSimulatorSession(
        connection: acceptanceConnection(), transport: http
    )
    #expect(session.endpoint == "https://example.test")
    #expect(session.expiresIn == 300)
    #expect(!session.allowInsecureLocalHTTP)
    let result = try await YoloTradeFullTestAcceptance.runSimulator(
        session: session, transport: http
    )
    #expect(result.status == "passed")
    #expect(result.iosClientOnline)
    #expect(result.modelRoundtripPassed)
    #expect(!result.businessToolsEnabled)
    #expect(!result.conversationSaved)

    let requests = await http.requests
    #expect(requests.map { $0.url?.path } == [
        "/v1/health", "/v1/admin/full-test/simulator-session", "/v1/health",
        "/v1/devices/pair", "/v1/models", "/v1/full-test/model-probe",
        "/v1/devices/10000000-0000-4000-8000-000000000009",
    ])
    #expect(requests[2].value(forHTTPHeaderField: "Authorization") == nil)
    #expect(requests[3].value(forHTTPHeaderField: "Authorization") == nil)
    #expect(requests[4...].allSatisfy {
        $0.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-ios-token"
    })
    #expect(String(decoding: requests[3].httpBody ?? Data(), as: UTF8.self).contains("\"platform\":\"ios\""))
    #expect(String(decoding: requests[5].httpBody ?? Data(), as: UTF8.self).contains("fixture-probe-grant"))
    let encoded = String(decoding: try JSONEncoder().encode(result), as: UTF8.self)
    #expect(!encoded.contains("fixture-probe-grant"))
    #expect(!encoded.contains("fixture-ios-token"))
}

private func acceptanceConnection() throws -> PairedConnection {
    let data = try JSONSerialization.data(withJSONObject: [
        "device": [
            "id": "10000000-0000-4000-8000-000000000001", "name": "Mac",
            "platform": "macos", "permissions": ["chat:read", "chat:write", "devices:manage"],
            "created_at": "2026-11-06T00:00:00Z", "revoked_at": NSNull(),
        ],
        "token": "fixture-token", "server_id": "40000000-0000-4000-8000-000000000004",
        "api_version": 1,
    ])
    let pairing = try WireJSON.decoder().decode(PairingResponse.self, from: data)
    return PairedConnection(
        endpoint: try ServiceEndpoint("https://example.test"),
        pairing: pairing,
        agentID: YoloTradeProduct.agentID
    )
}

@Test func fullTestProbeUsesStoredIdentityWithoutExposingTokenOrEnablingBusinessTools() async throws {
    let http = AcceptanceHTTP()
    let result = try await YoloTradeFullTestAcceptance.run(
        connection: acceptanceConnection(), transport: http
    )
    #expect(result.status == "passed")
    #expect(result.modelRoundtripPassed)
    #expect(result.macClientOnline)
    #expect(!result.businessToolsEnabled)
    #expect(!result.conversationSaved)
    let requests = await http.requests
    #expect(requests.count == 5)
    #expect(requests.first?.value(forHTTPHeaderField: "Authorization") == nil)
    #expect(requests.dropFirst().allSatisfy {
        $0.value(forHTTPHeaderField: "Authorization") == "Bearer fixture-token"
    })
    #expect(requests.last?.httpBody == Data("{}".utf8))
    let encoded = String(decoding: try JSONEncoder().encode(result), as: UTF8.self)
    #expect(!encoded.contains("fixture-token"))
}
