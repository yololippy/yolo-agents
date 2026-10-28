import Foundation
import SwiftUI
import YoloAgentsSDK

public enum AccountAccess {
    public static var currentPlatform: DevicePlatform {
        #if os(macOS)
        .macos
        #else
        .ios
        #endif
    }
    public static func canManage(platform: DevicePlatform, device: Device, serverAllows: Bool?) -> Bool {
        platform == .macos && device.platform == .macos &&
            device.permissions.contains("devices:manage") && device.revokedAt == nil && serverAllows != false
    }
}

@MainActor
public final class ChatGPTAccountModel: ObservableObject {
    @Published public private(set) var account: ChatGPTAccountStatus?
    @Published public private(set) var isLoading = false
    @Published public private(set) var isManaging = false
    @Published public private(set) var isWaitingForLogin = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var noticeMessage: String?
    private let client: APIClient
    private let connection: PairedConnection
    private let identity: AgentAppIdentity?
    private let platform: DevicePlatform
    private let pollInterval: Duration
    private let loginTimeout: Duration
    private var route: AccountRoute?
    private var loginID: String?
    private var pollingTask: Task<Void, Never>?
    private var generation = UUID()

    public init(connection: PairedConnection, identity: AgentAppIdentity? = nil, client: APIClient? = nil,
                platform: DevicePlatform = AccountAccess.currentPlatform,
                pollInterval: Duration = .seconds(2), loginTimeout: Duration = .seconds(600)) {
        self.client = client ?? APIClient(endpoint: connection.endpoint, token: connection.pairing.token)
        self.connection = connection; self.identity = identity; self.platform = platform
        self.pollInterval = pollInterval; self.loginTimeout = loginTimeout
    }
    private var managerDevice: Bool {
        #if os(macOS)
        AccountAccess.canManage(platform: platform, device: connection.pairing.device, serverAllows: nil)
        #else
        false
        #endif
    }
    public var canManage: Bool {
        managerDevice && route != nil && (route == .codexLegacy ||
            (account?.canManage == true && ["chatgpt", "codex"].contains(account?.method ?? ""))) && AccountAccess.canManage(platform: platform,
            device: connection.pairing.device, serverAllows: account?.canManage)
    }
    public var appName: String { identity?.appName ?? account?.appName ?? "当前 Agent" }
    public var usesCodex: Bool { route == .codexLegacy || account?.method == "codex" }
    public var hasPendingLogin: Bool { loginID != nil || ["pending", "login_pending"].contains(account?.status ?? "") }
    public var canLogout: Bool {
        canManage && (hasPendingLogin || ["authenticated", "logged_in", "expired", "permission_required"].contains(account?.status ?? ""))
    }
    public var logoutExplanation: String {
        usesCodex ? "当前服务使用 Codex 受管登录。退出会影响该服务所连接的 Codex 登录及使用它的其他会话。" :
        "仅退出 \(appName) 的 ChatGPT 授权并清除其本地登录，其他 Agent 的登录和聊天历史保持独立。"
    }
    public var statusLabel: String {
        switch account?.status {
        case "authenticated", "logged_in": "已登录"
        case "login_required", "logged_out", "unauthenticated": "未登录"
        case "pending", "login_pending": "等待浏览器登录"
        case "expired": "登录状态需要刷新"
        case "permission_required": "需要授予套餐使用权限"
        case nil: "尚未读取"
        default: "暂不可用"
        }
    }
    private func validateHealth() async throws {
        guard let identity else { return }
        try identity.validate(connection)
        let health = try await client.health()
        try identity.validate(health, serverID: connection.pairing.serverID)
    }
    private func validateAccount(_ value: ChatGPTAccountStatus, route: AccountRoute) throws {
        guard let identity else { return }
        if route == .codexLegacy && identity.allowsLegacyServer {
            if let id = value.agentID, id != identity.agentID { throw ClientError.agentIdentityMismatch }
        } else {
            guard value.agentID == identity.agentID else { throw ClientError.agentIdentityMismatch }
        }
    }
    public func refresh() async {
        guard !isLoading, !isManaging else { return }
        let attempt = generation
        isLoading = true
        defer { if generation == attempt { isLoading = false } }
        do {
            try await validateHealth()
            let value: ChatGPTAccountStatus
            if managerDevice {
                var selectedRoute = route ?? .agent
                do { value = try await client.account(route: selectedRoute) }
                catch ClientError.http(let status, _, _) where status == 404 && selectedRoute == .agent && (identity?.allowsLegacyServer ?? true) {
                    // Discover a missing read endpoint only. Never replay a POST on another route.
                    selectedRoute = .codexLegacy
                    value = try await client.account(route: selectedRoute)
                }
                try validateAccount(value, route: selectedRoute)
                try check(attempt)
                route = selectedRoute
            } else {
                let catalog = try await client.models()
                let status = catalog.auth?.status ?? (catalog.auth?.authenticated == true ? "authenticated" : "unavailable")
                // Do not copy private account data, even if the catalog contains extra fields.
                value = ChatGPTAccountStatus(status: status, canManage: false, error: catalog.error)
            }
            try check(attempt)
            account = value; errorMessage = value.error?.message
        } catch {
            guard generation == attempt, !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
            if let error = error as? ClientError, error.requiresPairing { route = nil }
        }
    }
    public func beginLogin() async -> URL? {
        guard managerDevice, !isManaging, !hasPendingLogin else { return nil }
        if route == nil { await refresh() }
        guard canManage, !hasPendingLogin, let route else { return nil }
        isManaging = true
        let attempt = generation
        defer { if generation == attempt { isManaging = false } }
        do {
            try await validateHealth()
            try check(attempt)
            let response = try await client.startAccountLogin(route: route)
            try check(attempt)
            if let error = response.error { errorMessage = error.message; return nil }
            if ["authenticated", "already_authenticated"].contains(response.status ?? "") {
                isManaging = false; await refresh(); return nil
            }
            loginID = response.loginID
            guard let loginID, !loginID.isEmpty, let url = try response.authorizationURL() else {
                throw ClientError.untrustedAuthorizationURL
            }
            errorMessage = nil; noticeMessage = nil; isWaitingForLogin = true
            return url
        } catch {
            guard generation == attempt, !Task.isCancelled else { return nil }
            isManaging = false
            if loginID != nil { await cancelLogin() }
            errorMessage = error.localizedDescription
            return nil
        }
    }
    public func browserOpened(_ opened: Bool) {
        guard isWaitingForLogin else { return }
        guard opened else {
            Task { [weak self] in
                await self?.cancelLogin()
                self?.errorMessage = "无法打开浏览器，已请求取消本次登录。"
            }
            return
        }
        guard pollingTask == nil else { return }
        let attempt = generation
        pollingTask = Task { [weak self] in
            guard let self else { return }
            let deadline = ContinuousClock.now.advanced(by: self.loginTimeout)
            while !Task.isCancelled && self.generation == attempt {
                await self.refresh()
                guard !Task.isCancelled && self.generation == attempt else { return }
                if self.account?.isAuthenticated == true {
                    self.loginID = nil; self.isWaitingForLogin = false; self.pollingTask = nil; return
                }
                if self.account?.error != nil, !["pending", "login_pending"].contains(self.account?.status ?? "") {
                    self.pollingTask = nil
                    await self.cancelLogin()
                    return
                }
                if ContinuousClock.now >= deadline {
                    self.pollingTask = nil
                    await self.cancelLogin()
                    self.errorMessage = self.isWaitingForLogin ? "等待登录超时，服务端取消尚未确认，请重试取消。" : "等待登录超时，本次登录已取消。"
                    return
                }
                do { try await Task.sleep(for: self.pollInterval) } catch { return }
            }
        }
    }
    public func cancelLogin() async {
        guard managerDevice, !isManaging, let route, hasPendingLogin else { return }
        stopWaiting()
        isManaging = true
        let attempt = generation
        defer { if generation == attempt { isManaging = false } }
        do {
            try await validateHealth()
            try check(attempt)
            let result = try await client.cancelAccountLogin(route: route)
            try check(attempt)
            guard result.isCancelled else { throw ClientError.http(status: 409, code: "cancellation_unconfirmed", message: "服务端尚未确认取消登录。") }
            if let value = result.account {
                try validateAccount(value, route: route)
                account = value
            }
            self.loginID = nil; isWaitingForLogin = false; noticeMessage = "本次登录等待已取消。"
            errorMessage = result.account?.error?.code == "login_cancelled" ? nil : result.account?.error?.message
        } catch {
            guard generation == attempt else { return }
            isWaitingForLogin = true
            errorMessage = "取消登录尚未确认：\(error.localizedDescription)"
        }
    }
    public func logout() async {
        guard canManage, !isManaging, let route else { return }
        stopWaiting()
        isManaging = true
        let attempt = generation
        defer { if generation == attempt { isManaging = false } }
        do {
            try await validateHealth()
            try check(attempt)
            let value = try await client.logoutAccount(route: route)
            try check(attempt); try validateAccount(value, route: route)
            account = value; errorMessage = value.error?.message
            loginID = nil; isWaitingForLogin = false
            if !value.isAuthenticated {
                noticeMessage = value.remoteRevocationConfirmed == true ? "已退出 \(appName)，远程授权已撤销。" : "已退出 \(appName) 的本地登录；远程授权撤销尚未确认。"
            }
        } catch {
            if generation == attempt, !Task.isCancelled { errorMessage = error.localizedDescription }
        }
    }
    /// Suspends local polling only. User-visible cancellation must call cancelLogin().
    public func stopWaiting() {
        pollingTask?.cancel(); pollingTask = nil
        generation = UUID(); isWaitingForLogin = loginID != nil; isLoading = false
    }
    private func check(_ attempt: UUID) throws {
        try Task.checkCancellation()
        if generation != attempt { throw CancellationError() }
    }
}

public typealias CodexAccountModel = ChatGPTAccountModel
