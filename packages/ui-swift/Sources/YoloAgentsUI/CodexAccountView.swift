import SwiftUI
import YoloAgentsSDK

private typealias AccountViewState<Value> = SwiftUI.State<Value>

struct CodexAccountView: View {
    @StateObject private var model: CodexAccountModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase
    @AccountViewState private var confirmingLogout = false
    let onAccountChanged: () -> Void

    init(connection: PairedConnection, onAccountChanged: @escaping () -> Void) {
        _model = StateObject(wrappedValue: CodexAccountModel(connection: connection))
        self.onAccountChanged = onAccountChanged
    }
    var body: some View {
        NavigationStack {
            Form {
                Section("账号状态") {
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
                        } label: { Label("登录 Codex 账号", systemImage: "person.crop.circle.badge.plus") }
                        .disabled(model.account?.isAuthenticated == true || model.isLoading || model.isManaging || model.isWaitingForLogin)

                        if model.isWaitingForLogin {
                            HStack {
                                ProgressView().controlSize(.small)
                                Text("请在浏览器完成官方登录，完成后会自动更新。")
                            }
                            Button("停止等待") { model.stopWaiting() }
                        }
                        if model.account?.isAuthenticated == true || model.isWaitingForLogin || model.account?.status == "login_pending" {
                            Button("退出 Codex 账号", role: .destructive) { confirmingLogout = true }
                                .disabled(model.isLoading || model.isManaging)
                        }
                        Text("登录与退出会影响此 Mac 上的 Codex 受管账号。")
                            .font(.footnote).foregroundStyle(.secondary)
                    } else {
                        Text("此设备仅可查看账号状态。请在具有管理权限的 Mac 上登录或退出 Codex 账号。")
                            .foregroundStyle(.secondary)
                    }
                }
                if let error = model.errorMessage {
                    Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red).textSelection(.enabled) }
                }
            }
            .formStyle(.grouped)
            .navigationTitle("Codex 账号")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .primaryAction) {
                    Button { Task { await model.refresh() } } label: { Label("刷新状态", systemImage: "arrow.clockwise") }
                        .disabled(model.isLoading || model.isManaging)
                }
            }
            .confirmationDialog("退出本机 Codex 账号？", isPresented: $confirmingLogout) {
                Button("退出 Codex 账号", role: .destructive) { Task { await model.logout() } }
                Button("取消", role: .cancel) {}
            } message: {
                Text("这会退出本机 Codex 的受管登录，其他使用该登录的会话也可能需要重新登录。")
            }
        }
        .task { await model.refresh() }
        .onChange(of: model.account?.status) { _, _ in onAccountChanged() }
        .onChange(of: scenePhase) { _, phase in
            #if os(macOS)
            if phase == .background { model.stopWaiting() }
            #else
            if phase != .active { model.stopWaiting() }
            #endif
        }
        .onDisappear { model.stopWaiting() }
        #if os(macOS)
        .frame(minWidth: 520, idealWidth: 560, minHeight: 420)
        #endif
    }
}
