import Foundation
import Testing
@testable import YoloAgentsSDK

private actor ModelHTTP: HTTPTransport {
    let response: Data
    private(set) var requests: [URLRequest] = []
    init(_ response: String) { self.response = Data(response.utf8) }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request); return HTTPResult(data: response, status: 200)
    }
}

@Test func actualModelCatalogNamesAndLoginStateDecode() throws {
    let data = Data("""
    {"models":[{"id":"gpt-fixture","display_name":"测试模型","description":"catalog only","is_default":true}],
     "default_model":"gpt-fixture","catalog_only":true,"auth":{"status":"login_required","method":null}}
    """.utf8)
    let catalog = try WireJSON.decoder().decode(ModelCatalog.self, from: data)
    #expect(catalog.models.first?.name == "测试模型")
    #expect(catalog.defaultModel == "gpt-fixture")
    #expect(catalog.auth?.needsLogin == true)
}

@Test func successfulHTTPModelCatalogCanStillContainServiceError() throws {
    let catalog = try WireJSON.decoder().decode(ModelCatalog.self, from: Data("""
    {"models":[],"default_model":null,"auth":{"status":"unavailable","method":null},
     "error":{"code":"codex_unavailable","message":"请检查登录状态"}}
    """.utf8))
    #expect(catalog.error?.message == "请检查登录状态")
}

@Test func legacyConversationWithoutModelDecodesAndNewModelPersists() throws {
    let id = UUID()
    let old = """
    {"id":"\(id)","title":"old","created_at":"2026-10-27T00:00:00Z","updated_at":"2026-10-27T00:00:00Z","archived":false}
    """
    var conversation = try WireJSON.decoder().decode(Conversation.self, from: Data(old.utf8))
    #expect(conversation.model == nil)
    conversation.model = "fixture-model"
    var state = ClientState(); state.upsertConversation(conversation)
    let restored = try WireJSON.decoder().decode(ClientState.self, from: WireJSON.encoder().encode(state))
    #expect(restored.conversations.first?.model == "fixture-model")
}

@Test func modelPatchSeparatesOmittedFieldFromExplicitDefaultNull() async throws {
    let id = UUID()
    let transport = ModelHTTP("""
    {"conversation":{"id":"\(id)","title":"chat","model":null,"archived":false,
    "created_at":"2026-10-27T00:00:00Z","updated_at":"2026-10-27T00:00:00Z"}}
    """)
    let client = APIClient(endpoint: try ServiceEndpoint("https://example.com"), token: "fixture", transport: transport)
    _ = try await client.updateConversation(id, title: "rename")
    _ = try await client.updateConversation(id, model: .model("chosen-model"))
    _ = try await client.updateConversation(id, model: .serverDefault)
    let requests = await transport.requests
    let bodies = try requests.map { request -> [String: Any] in
        let data = try #require(request.httpBody)
        return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }
    #expect(bodies[0]["model"] == nil)
    #expect(bodies[1]["model"] as? String == "chosen-model")
    #expect(bodies[2]["model"] is NSNull)
}
