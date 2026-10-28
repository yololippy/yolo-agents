import SwiftUI
import YoloAgentsSDK

private typealias AccountViewState<Value> = SwiftUI.State<Value>

struct ChatGPTAccountView: View {
    @StateObject private var model: ChatGPTAccountModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase
    @AccountViewState private var confirmingLogout = false
    let onAccountChanged: () -> Void

    init(connection: PairedConnection, identity: AgentAppIdentity?, onAccountChanged: @escaping () -> Void) {
        _model = StateObject(wrappedValue: ChatGPTAccountModel(connection: connection, identity: identity))
        self.onAccountChanged = onAccountChanged
    }
    var body: some View {
        NavigationStack {
            Form {
                Section("账号状态") {
                    LabeledContent("所属应用", value: model.appName).accessibilityIdentifier("account.app-name")
                    LabeledContent("状态") {
                        HStack {
                            if model.isLoading { ProgressView().controlSize(.small) }
                            Text(model.statusLabel)
                        }
                    }
                    if let account = model.account, account.isAuthenticated, model.canManage {
                        LabeledContent("邮箱") { Text(account.email ?? "服务未提供").textSelection(.enabled) }
                        LabeledContent("套餐", value: account.planType ?? "服务未提供")
                    }
                }
                Section("登录管理") {
                    if model.canManage {
                        Button {
                            Task {
                                guard let url = await model.beginLogin() else { return }
                                openURL(url) { opened in model.browserOpened(opened) }
                            }
                        } label: { Label("使用 ChatGPT 登录", systemImage: "person.crop.circle.badge.plus") }
                        .disabled(model.account?.isAuthenticated == true || model.isLoading || model.isManaging || model.hasPendingLogin)
                        .accessibilityIdentifier("account.login")

                        if model.hasPendingLogin {
                            HStack {
                                ProgressView().controlSize(.small)
                                Text("请在浏览器完成官方登录，完成后会自动更新。")
                            }
                            Button("取消本次登录") { Task { await model.cancelLogin() } }
                                .disabled(model.isManaging).accessibilityIdentifier("account.cancel-login")
                        }
                        if model.canLogout {
                            Button("退出当前 Agent 的登录", role: .destructive) { confirmingLogout = true }
                                .disabled(model.isLoading || model.isManaging)
                                .accessibilityIdentifier("account.logout")
                        }
                        Text(model.logoutExplanation)
                            .font(.footnote).foregroundStyle(.secondary)
                        if model.account?.status == "expired" {
                            Text("登录状态需要刷新，继续使用模型时可能恢复。也可重新登录或退出当前 Agent。")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        if model.account?.status == "permission_required" {
                            Text("已完成身份登录，尚未授予此 App 使用 ChatGPT 套餐的权限。可重新登录授权或退出。")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                    } else {
                        Text("此设备仅可查看脱敏登录状态。请在 \(model.appName) 的 Mac 客户端管理 ChatGPT 登录。")
                            .foregroundStyle(.secondary)
                    }
                }
                if let error = model.errorMessage {
                    Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red).textSelection(.enabled) }
                }
                if let notice = model.noticeMessage {
                    Section { Text(notice).foregroundStyle(.secondary).accessibilityIdentifier("account.notice") }
                }
            }
            .formStyle(.grouped)
            .navigationTitle("ChatGPT 账号")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("完成") {
                        Task {
                            if model.isWaitingForLogin { await model.cancelLogin() }
                            if !model.isWaitingForLogin { dismiss() }
                        }
                    }.disabled(model.isManaging)
                }
                ToolbarItem(placement: .primaryAction) {
                    Button { Task { await model.refresh() } } label: { Label("刷新状态", systemImage: "arrow.clockwise") }
                        .disabled(model.isLoading || model.isManaging)
                }
            }
            .confirmationDialog("退出 \(model.appName) 的登录？", isPresented: $confirmingLogout) {
                Button("确认退出", role: .destructive) { Task { await model.logout() } }
                Button("取消", role: .cancel) {}
            } message: {
                Text(model.logoutExplanation)
            }
        }
        .interactiveDismissDisabled(model.isManaging)
        .task { await model.refresh() }
        .onChange(of: model.account?.status) { _, _ in onAccountChanged() }
        .onChange(of: scenePhase) { _, phase in
            #if os(macOS)
            if phase == .background, model.isWaitingForLogin, !model.isManaging { model.stopWaiting() }
            if phase == .active {
                Task {
                    await model.refresh()
                    if model.isWaitingForLogin { model.browserOpened(true) }
                }
            }
            #else
            if phase != .active, !model.isManaging { model.stopWaiting() }
            #endif
        }
        .onDisappear {
            Task {
                if model.isWaitingForLogin { await model.cancelLogin() }
                model.stopWaiting()
            }
        }
        #if os(macOS)
        .frame(minWidth: 520, idealWidth: 560, minHeight: 420)
        #endif
    }
}
