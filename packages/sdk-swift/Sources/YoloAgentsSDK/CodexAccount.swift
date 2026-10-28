import Foundation

public enum AccountRoute: Sendable {
    case agent, codexLegacy
    var path: String { self == .agent ? "account" : "codex/account" }
}

public struct ChatGPTAccountStatus: Decodable, Equatable, Sendable {
    public let status: String
    public let email: String?
    public let planType: String?
    public let method: String?
    public let canManage: Bool?
    public let error: ServiceError?
    public let agentID: String?
    public let appName: String?
    public let remoteRevocationConfirmed: Bool?
    public var isAuthenticated: Bool { ["authenticated", "logged_in"].contains(status) }
    public init(status: String, email: String? = nil, planType: String? = nil, method: String? = nil,
                canManage: Bool? = nil, error: ServiceError? = nil, agentID: String? = nil,
                appName: String? = nil, remoteRevocationConfirmed: Bool? = nil) {
        self.status = status; self.email = email; self.planType = planType; self.method = method
        self.canManage = canManage; self.error = error
        self.agentID = agentID; self.appName = appName; self.remoteRevocationConfirmed = remoteRevocationConfirmed
    }
    private enum CodingKeys: String, CodingKey {
        case status, email, method, account, auth, error, authenticated
        case planType = "plan_type", plan, authMethod = "auth_method", canManage = "can_manage"
        case agentID = "agent_id", appName = "app_name", remoteRevocationConfirmed = "remote_revocation_confirmed"
    }
    public init(from decoder: Decoder) throws {
        let root = try decoder.container(keyedBy: CodingKeys.self)
        let values: KeyedDecodingContainer<CodingKeys>
        if root.contains(.account), (try? root.decodeNil(forKey: .account)) == false {
            values = try root.nestedContainer(keyedBy: CodingKeys.self, forKey: .account)
        } else if root.contains(.auth), (try? root.decodeNil(forKey: .auth)) == false {
            values = try root.nestedContainer(keyedBy: CodingKeys.self, forKey: .auth)
        } else { values = root }
        if let state = try values.decodeIfPresent(String.self, forKey: .status) { status = state }
        else if let loggedIn = try values.decodeIfPresent(Bool.self, forKey: .authenticated) { status = loggedIn ? "authenticated" : "login_required" }
        else { throw DecodingError.keyNotFound(CodingKeys.status, .init(codingPath: decoder.codingPath, debugDescription: "Missing account status")) }
        email = try values.decodeIfPresent(String.self, forKey: .email)
        planType = try values.decodeIfPresent(String.self, forKey: .planType) ?? values.decodeIfPresent(String.self, forKey: .plan)
        method = try values.decodeIfPresent(String.self, forKey: .method) ?? values.decodeIfPresent(String.self, forKey: .authMethod)
        canManage = try root.decodeIfPresent(Bool.self, forKey: .canManage) ?? values.decodeIfPresent(Bool.self, forKey: .canManage)
        let issue = try root.decodeIfPresent(AccountIssue.self, forKey: .error) ?? values.decodeIfPresent(AccountIssue.self, forKey: .error)
        error = issue.map { ServiceError(code: $0.code, message: $0.message ?? Self.issueMessage($0.code)) }
        agentID = try values.decodeIfPresent(String.self, forKey: .agentID)
        appName = try values.decodeIfPresent(String.self, forKey: .appName)
        remoteRevocationConfirmed = try values.decodeIfPresent(Bool.self, forKey: .remoteRevocationConfirmed) ?? root.decodeIfPresent(Bool.self, forKey: .remoteRevocationConfirmed)
    }
    private struct AccountIssue: Decodable { let code: String; let message: String? }
    private static func issueMessage(_ code: String) -> String {
        switch code {
        case "remote_revocation_unconfirmed": "当前 Agent 的本地登录已清除，远程授权撤销尚未确认。"
        case "login_cancelled": "登录等待已取消。"
        case "login_expired": "登录已过期，请重新发起。"
        case "login_denied": "浏览器授权未完成，请重新登录。"
        case "plan_usage_not_granted": "尚未授予 ChatGPT 套餐使用权限。"
        default: "账号操作未完成（\(code)）。"
        }
    }
}

public struct ChatGPTLoginResponse: Decodable, Equatable, Sendable {
    public let status: String?
    public let authURL: String?
    public let loginID: String?
    public let error: ServiceError?
    enum CodingKeys: String, CodingKey {
        case status, error
        case authURL = "auth_url", loginID = "login_id"
    }
    public func authorizationURL() throws -> URL? {
        guard let authURL else { return nil }
        guard !authURL.isEmpty, authURL.utf8.count <= 16_384,
              authURL == authURL.trimmingCharacters(in: .whitespacesAndNewlines),
              !authURL.contains("\\"), !authURL.unicodeScalars.contains(where: { $0.value < 33 || $0.value == 127 }),
              let components = URLComponents(string: authURL), let url = components.url,
              components.scheme?.lowercased() == "https",
              ["chatgpt.com", "auth.openai.com"].contains(components.host?.lowercased() ?? ""),
              components.port == nil || components.port == 443,
              components.user == nil, components.password == nil, components.fragment == nil else {
            throw ClientError.untrustedAuthorizationURL
        }
        let secretNames: Set<String> = ["access_token", "refresh_token", "id_token", "token", "api_key"]
        guard !(components.queryItems ?? []).contains(where: { secretNames.contains($0.name.lowercased()) }) else {
            throw ClientError.untrustedAuthorizationURL
        }
        return url
    }
}

public struct AccountLoginCancellation: Decodable, Sendable {
    public let status: String?
    public let cancelled: Bool?
    public let account: ChatGPTAccountStatus?
    public var isCancelled: Bool {
        cancelled == true || ["cancelled", "login_cancelled"].contains(status ?? "") ||
        ["login_required", "authenticated", "logged_in", "logged_out"].contains(account?.status ?? "")
    }
}

// Source compatibility for existing clients; new account routes are provider-neutral.
public typealias CodexAccountStatus = ChatGPTAccountStatus
public typealias CodexLoginResponse = ChatGPTLoginResponse
