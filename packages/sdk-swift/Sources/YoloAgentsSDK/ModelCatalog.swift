import Foundation

public struct ModelOption: Decodable, Equatable, Sendable, Identifiable {
    public let id: String
    public let name: String
    public let provider: String?
    public init(id: String, name: String, provider: String? = nil) {
        self.id = id; self.name = name; self.provider = provider
    }
    private enum CodingKeys: String, CodingKey {
        case id, model, name, provider, displayName
        case displayNameSnake = "display_name"
    }
    public init(from decoder: Decoder) throws {
        if let value = try? decoder.singleValueContainer().decode(String.self) {
            self.init(id: value, name: value); return
        }
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let id = try values.decodeIfPresent(String.self, forKey: .id) ?? values.decode(String.self, forKey: .model)
        let name = try values.decodeIfPresent(String.self, forKey: .name) ??
            values.decodeIfPresent(String.self, forKey: .displayNameSnake) ??
            values.decodeIfPresent(String.self, forKey: .displayName) ?? id
        self.init(id: id, name: name, provider: try values.decodeIfPresent(String.self, forKey: .provider))
    }
}

/// Display information only: a catalog entry does not prove inference entitlement.
public struct ModelAuthentication: Decodable, Equatable, Sendable {
    public let status: String?
    public let authenticated: Bool?
    public let message: String?
    private enum CodingKeys: String, CodingKey {
        case status, state, authenticated, message
        case isAuthenticated = "is_authenticated", loginRequired = "login_required"
    }
    public init(from decoder: Decoder) throws {
        let single = try decoder.singleValueContainer()
        if let value = try? single.decode(Bool.self) {
            status = nil; authenticated = value; message = nil; return
        }
        if let value = try? single.decode(String.self) {
            status = value; authenticated = nil; message = nil; return
        }
        let values = try decoder.container(keyedBy: CodingKeys.self)
        status = try values.decodeIfPresent(String.self, forKey: .status) ?? values.decodeIfPresent(String.self, forKey: .state)
        let loggedIn = try values.decodeIfPresent(Bool.self, forKey: .authenticated) ??
            values.decodeIfPresent(Bool.self, forKey: .isAuthenticated)
        let loginRequired = try values.decodeIfPresent(Bool.self, forKey: .loginRequired)
        authenticated = loggedIn ?? loginRequired.map { !$0 }
        message = try values.decodeIfPresent(String.self, forKey: .message)
    }
    public var needsLogin: Bool {
        authenticated == false || ["login_required", "unauthenticated", "not_logged_in"].contains(status ?? "")
    }
}

public struct ModelCatalog: Decodable, Equatable, Sendable {
    public let models: [ModelOption]
    public let defaultModel: String?
    public let auth: ModelAuthentication?
    public let error: ServiceError?
    private enum CodingKeys: String, CodingKey {
        case models, auth, error
        case defaultModel = "default_model", authStatus = "auth_status"
    }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        models = try values.decode([ModelOption].self, forKey: .models)
        defaultModel = try values.decodeIfPresent(String.self, forKey: .defaultModel)
        auth = try values.decodeIfPresent(ModelAuthentication.self, forKey: .auth) ??
            values.decodeIfPresent(ModelAuthentication.self, forKey: .authStatus)
        error = try values.decodeIfPresent(ServiceError.self, forKey: .error)
    }
}

public enum ConversationModelSelection: Equatable, Sendable {
    case serverDefault
    case model(String)
}
