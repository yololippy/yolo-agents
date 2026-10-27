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
public final class CodexAccountModel: ObservableObject {
    @Published public private(set) var account: CodexAccountStatus?
    @Published public private(set) var isLoading = false
    @Published public private(set) var isManaging = false
    @Published public private(set) var isWaitingForLogin = false
    @Published public private(set) var errorMessage: String?
    private let client: APIClient
    private let device: Device
    private let platform: DevicePlatform
    private let pollInterval: Duration
    private let loginTimeout: Duration
    private var pollingTask: Task<Void, Never>?
    private var generation = UUID()

    public init(connection: PairedConnection, client: APIClient? = nil,
                platform: DevicePlatform = AccountAccess.currentPlatform,
                pollInterval: Duration = .seconds(2), loginTimeout: Duration = .seconds(600)) {
        self.client = client ?? APIClient(endpoint: connection.endpoint, token: connection.pairing.token)
        device = connection.pairing.device; self.platform = platform
        self.pollInterval = pollInterval; self.loginTimeout = loginTimeout
    }
    public var canManage: Bool { AccountAccess.canManage(platform: platform, device: device, serverAllows: account?.canManage) }
    public var statusLabel: String {
        guard let account else { return "尚未读取" }
        switch account.status {
        case "authenticated", "logged_in": return "已登录"
        case "login_required", "logged_out", "unauthenticated": return "未登录"
        case "pending", "login_pending": return "等待浏览器登录"
        default: return "暂不可用"
        }
    }
    public func refresh() async {
        guard !isLoading else { return }
        let attempt = generation
        isLoading = true
        defer { if generation == attempt { isLoading = false } }
        do {
            let value: CodexAccountStatus
            if AccountAccess.canManage(platform: platform, device: device, serverAllows: nil) {
                value = try await client.account()
            } else {
                // The private account endpoints are Mac-manager only. Other devices
                // use the already-authorized catalog's redacted authentication state.
                let catalog = try await client.models()
                let status = catalog.auth?.status ?? (catalog.auth?.authenticated == true ? "authenticated" : "unavailable")
                value = CodexAccountStatus(status: status, canManage: false, error: catalog.error)
            }
            try check(attempt)
            account = value
            errorMessage = value.error?.message
        } catch {
            guard generation == attempt, !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
        }
    }
    public func beginLogin() async -> URL? {
        guard canManage, !isManaging, !isWaitingForLogin else { return nil }
        isManaging = true
        let attempt = generation
        defer { if generation == attempt { isManaging = false } }
        do {
            let response = try await client.startAccountLogin()
            try check(attempt)
            if let error = response.error {
                errorMessage = error.message; return nil
            }
            if ["authenticated", "already_authenticated"].contains(response.status ?? "") {
                await refresh(); return nil
            }
            guard let url = try response.authorizationURL() else {
                errorMessage = "服务没有返回官方登录地址，请在 Mac 检查账号服务。"; return nil
            }
            errorMessage = nil
            isWaitingForLogin = true
            return url
        } catch {
            if generation == attempt, !Task.isCancelled { errorMessage = error.localizedDescription }
            return nil
        }
    }
    public func browserOpened(_ opened: Bool) {
        guard isWaitingForLogin else { return }
        guard opened else {
            isWaitingForLogin = false
            errorMessage = "无法打开浏览器，请检查系统默认浏览器后重试。"
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
                    self.isWaitingForLogin = false; self.pollingTask = nil; return
                }
                if ContinuousClock.now >= deadline {
                    self.isWaitingForLogin = false; self.pollingTask = nil
                    self.errorMessage = "等待登录超时，请重新发起登录或刷新账号状态。"
                    return
                }
                do { try await Task.sleep(for: self.pollInterval) } catch { return }
            }
        }
    }
    /// Confirmation belongs to the view. This method never silently signs out during dismissal.
    public func logout() async {
        guard canManage, !isManaging else { return }
        stopWaiting()
        isManaging = true
        let attempt = generation
        defer { if generation == attempt { isManaging = false } }
        do {
            let value = try await client.logoutAccount()
            try check(attempt)
            account = value; errorMessage = value.error?.message
        } catch {
            if generation == attempt, !Task.isCancelled { errorMessage = error.localizedDescription }
        }
    }
    public func stopWaiting() {
        pollingTask?.cancel(); pollingTask = nil
        generation = UUID()
        isWaitingForLogin = false; isLoading = false; isManaging = false
    }
    private func check(_ attempt: UUID) throws {
        try Task.checkCancellation()
        if generation != attempt { throw CancellationError() }
    }
}
