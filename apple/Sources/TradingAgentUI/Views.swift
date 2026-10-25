import SwiftUI
import TradingAgentSDK

// Resolve the long-standing property wrapper explicitly. The macOS 27 SDK also
// exports a same-named macro whose plugin is absent from Command Line Tools.
private typealias ViewState<Value> = SwiftUI.State<Value>

public struct TradingAgentRootView: View {
    @StateObject private var model = ChatModel()
    @Environment(\.scenePhase) private var scenePhase
    public init() {}
    public var body: some View {
        Group {
            if !model.started {
                ProgressView("正在打开…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.connection == nil {
                PairingView(model: model, isSettings: false)
            } else {
                ChatShell(model: model)
            }
        }
        .task { await model.start() }
        .onChange(of: scenePhase) { _, phase in model.setActive(phase == .active) }
        .alert("操作未完成", isPresented: Binding(get: { model.errorMessage != nil }, set: {
            if !$0 { model.errorMessage = nil }
        })) {
            Button("知道了") { model.errorMessage = nil }
        } message: { Text(model.errorMessage ?? "") }
        #if os(macOS)
        .frame(minWidth: 760, minHeight: 560)
        #endif
    }
}

private struct PairingView: View {
    @ObservedObject var model: ChatModel
    let isSettings: Bool
    @Environment(\.dismiss) private var dismiss
    @ViewState private var address = ""
    @ViewState private var deviceName = ""
    @ViewState private var code = ""
    @ViewState private var allowHTTP = false
    @ViewState private var confirmingDisconnect = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Label("连接你的 Mac 助手", systemImage: "desktopcomputer.and.iphone")
                        .font(.title2.bold()).padding(.vertical, 8)
                    Text("在 Mac 服务端获取配对码，将聊天与历史同步到这台设备。")
                        .foregroundStyle(.secondary)
                }
                if let connection = model.connection, isSettings {
                    Section("当前连接") {
                        LabeledContent("服务", value: connection.endpoint.url.absoluteString)
                        LabeledContent("设备", value: connection.pairing.device.name)
                        LabeledContent("状态", value: model.connectionState.label)
                        Text("此客户端用于聊天与查看结果。交易权限由服务端管理。")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                Section(isSettings ? "重新配对" : "服务地址") {
                    TextField("https://你的服务地址", text: $address)
                        .textContentType(.URL)
                        #if os(iOS)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        #endif
                    TextField("设备名称", text: $deviceName)
                    SecureField("Mac 上显示的配对码", text: $code)
                        #if os(iOS)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        #endif
                    Toggle("允许本地开发连接（未加密）", isOn: $allowHTTP)
                    if allowHTTP {
                        Text("仅用于可信开发网络。HTTP 不加密，地址须为 localhost 或私有 IP；远程连接请使用 HTTPS。")
                            .font(.footnote).foregroundStyle(.orange)
                    }
                    Button {
                        Task {
                            if await model.pair(address: address, code: code, deviceName: deviceName, allowHTTP: allowHTTP) {
                                code = ""
                                if isSettings { dismiss() }
                            }
                        }
                    } label: {
                        HStack {
                            if model.busy { ProgressView().controlSize(.small) }
                            Text(model.busy ? "正在配对…" : "配对此设备")
                        }
                    }
                    .disabled(model.busy || address.isEmpty || code.isEmpty || deviceName.trimmingCharacters(in: .whitespaces).isEmpty)
                    .buttonStyle(.borderedProminent)
                }
                Section {
                    Text("离线时可查看此设备保存的最近聊天。配对资料与缓存仅保存在此设备。")
                        .font(.footnote).foregroundStyle(.secondary)
                    #if os(iOS)
                    Text("iPhone 上的 127.0.0.1 指向手机自身，请填写 Mac 的可达服务地址。")
                        .font(.footnote).foregroundStyle(.secondary)
                    #endif
                }
                if isSettings {
                    Section {
                        Button("断开并清除本机记录", role: .destructive) { confirmingDisconnect = true }
                        Text("这会移除本机配对和缓存。要撤销服务端设备令牌，请在 Mac 服务端撤销该设备。")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
            .formStyle(.grouped)
            .navigationTitle(isSettings ? "连接设置" : "Trading Agent")
            .toolbar {
                if isSettings { ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } } }
            }
            .confirmationDialog("清除本机配对和缓存？", isPresented: $confirmingDisconnect) {
                Button("清除本机记录", role: .destructive) { model.disconnect(); dismiss() }
            }
            .onAppear {
                address = model.connection?.endpoint.url.absoluteString ?? defaultAddress
                deviceName = model.connection?.pairing.device.name ?? defaultName
                allowHTTP = model.connection?.endpoint.allowInsecureLocalHTTP ?? false
            }
        }
        #if os(macOS)
        .frame(minWidth: 520, idealWidth: 580, minHeight: 640)
        #endif
    }
    private var defaultAddress: String {
        #if os(macOS)
        "http://127.0.0.1:8765"
        #else
        ""
        #endif
    }
    private var defaultName: String {
        #if os(macOS)
        Host.current().localizedName ?? "我的 Mac"
        #else
        "我的 iPhone"
        #endif
    }
}

private struct ChatShell: View {
    @ObservedObject var model: ChatModel
    @ViewState private var query = ""
    @ViewState private var includeArchived = false
    @ViewState private var showSettings = false
    @ViewState private var renaming: Conversation? = nil
    @ViewState private var newTitle = ""
    private var conversations: [Conversation] {
        model.state.conversations.filter {
            (includeArchived || !$0.archived) &&
            (query.isEmpty || $0.title.localizedCaseInsensitiveContains(query))
        }
    }
    var body: some View {
        NavigationSplitView {
            VStack(spacing: 0) {
                List(selection: $model.selectedID) {
                    ForEach(conversations) { conversation in
                        NavigationLink(value: conversation.id) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(conversation.title).lineLimit(1)
                                HStack {
                                    Text(conversation.updatedAt, style: .date)
                                    if conversation.archived { Image(systemName: "archivebox") }
                                }.font(.caption).foregroundStyle(.secondary)
                            }.padding(.vertical, 4)
                        }
                        .contextMenu {
                            Button("重命名") { newTitle = conversation.title; renaming = conversation }
                                .disabled(!model.isOnline)
                            Button(conversation.archived ? "取消归档" : "归档") {
                                Task { await model.updateConversation(conversation, archived: !conversation.archived) }
                            }.disabled(!model.isOnline)
                        }
                    }
                }
                Toggle("显示归档", isOn: $includeArchived)
                    .toggleStyle(.switch).font(.caption).padding()
                Button { showSettings = true } label: {
                    Label(model.connectionState.label, systemImage: "gearshape")
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).padding()
            }
            .navigationTitle("会话")
            .searchable(text: $query, prompt: "搜索已同步的会话标题")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { Task { await model.createConversation() } } label: {
                        Label("新会话", systemImage: "square.and.pencil")
                    }.disabled(!model.isOnline || model.busy)
                }
            }
        } detail: {
            ChatDetail(model: model, openSettings: { showSettings = true })
        }
        .sheet(isPresented: $showSettings) { PairingView(model: model, isSettings: true) }
        .alert("重命名会话", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("会话名称", text: $newTitle)
            Button("保存") {
                if let conversation = renaming {
                    Task { await model.updateConversation(conversation, title: newTitle.trimmingCharacters(in: .whitespacesAndNewlines)) }
                }
                renaming = nil
            }.disabled(newTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Button("取消", role: .cancel) { renaming = nil }
        }
    }
}

private struct ChatDetail: View {
    @ObservedObject var model: ChatModel
    let openSettings: () -> Void
    @ViewState private var drafts: [UUID: String] = [:]
    @FocusState private var composerFocused: Bool
    private var draft: Binding<String> {
        Binding(get: { model.selectedID.flatMap { drafts[$0] } ?? "" }, set: {
            if let id = model.selectedID { drafts[id] = $0 }
        })
    }
    var body: some View {
        VStack(spacing: 0) {
            connectionBanner
            if model.selectedID == nil {
                ContentUnavailableView {
                    Label("与你的助手聊聊", systemImage: "bubble.left.and.bubble.right")
                } description: {
                    Text(model.isOnline ? "选择已有会话，或创建一个新会话。" : "连接恢复后即可创建会话；已缓存的历史仍可浏览。")
                } actions: {
                    Button("新建会话") { Task { await model.createConversation() } }
                        .disabled(!model.isOnline || model.busy)
                }
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 18) {
                            ForEach(model.messages) { message in MessageBubble(message: message, model: model) }
                            ForEach(model.partialReplies) { reply in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("助手").font(.caption.bold()).foregroundStyle(.secondary)
                                    if !reply.content.isEmpty { Text(reply.content).textSelection(.enabled) }
                                    if let error = reply.error {
                                        Label("回复失败：\(error.message)", systemImage: "exclamationmark.triangle")
                                            .font(.caption).foregroundStyle(.red)
                                    } else {
                                        Label(model.isOnline ? "正在回复…" : "等待重新同步…", systemImage: "ellipsis.bubble")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                                .padding(14).background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
                            }
                            Color.clear.frame(height: 1).id("bottom")
                        }.padding().frame(maxWidth: 900, alignment: .leading).frame(maxWidth: .infinity)
                    }
                    .onChange(of: model.messages) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                    .onChange(of: model.partialReplies) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .task(id: model.selectedID) { await model.loadSelectedMessages() }
                Divider()
                HStack(alignment: .bottom, spacing: 12) {
                    TextField(model.isOnline ? "输入消息…" : "离线时可查看历史", text: draft, axis: .vertical)
                        .lineLimit(1...6).textFieldStyle(.plain).focused($composerFocused)
                        .padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                        .disabled(!model.isOnline || model.selectedConversation?.archived == true)
                    Button {
                        let content = draft.wrappedValue
                        let conversationID = model.selectedID
                        Task {
                            guard model.selectedID == conversationID else { return }
                            if await model.send(content), let id = conversationID, drafts[id] == content { drafts[id] = nil }
                        }
                    } label: {
                        Image(systemName: "arrow.up.circle.fill").font(.system(size: 30))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("发送消息")
                    .disabled(!model.isOnline || model.busy || model.selectedConversation?.archived == true ||
                              draft.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }.padding()
            }
        }
        .navigationTitle(model.selectedConversation?.title ?? "Trading Agent")
        .toolbar {
            ToolbarItem(placement: .primaryAction) { Button(action: openSettings) { Label("连接设置", systemImage: "gearshape") } }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            Text("仅聊天与查看 · 数据以 Mac 服务端为准")
                .font(.caption2).foregroundStyle(.secondary).padding(.bottom, 6)
        }
    }
    private var connectionBanner: some View {
        HStack(spacing: 8) {
            Circle().fill(model.isOnline ? Color.green : Color.orange).frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.connectionState.label).font(.caption.bold())
                if case .offline(let reason) = model.connectionState { Text(reason).font(.caption2).lineLimit(2) }
                if let health = model.health {
                    Text(health.agentMode == "demo" ? "演示模式 · 未调用真实模型或券商" :
                         "助手模式：\(health.agentMode)\(health.model.map { " · \($0)" } ?? "")")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if model.connectionState == .authorizationRequired {
                Button("重新配对", action: openSettings).font(.caption)
            } else if !model.isOnline {
                Button("重连") { model.reconnect() }.font(.caption)
            }
        }.padding(.horizontal).padding(.vertical, 8).background(.bar)
    }
}

private struct MessageBubble: View {
    let message: Message
    @ObservedObject var model: ChatModel
    private var outgoing: OutgoingMessage? {
        model.state.outgoing.first { $0.id == message.clientMessageID }
    }
    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 35) }
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(message.role == .user ? "你" : message.role == .assistant ? "助手" : "系统")
                    Spacer()
                    Text(message.createdAt, style: .time)
                }.font(.caption).foregroundStyle(.secondary)
                Text(message.content).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                if let outgoing {
                    switch outgoing.status {
                    case .submitting: Text("正在发送…").font(.caption).foregroundStyle(.secondary)
                    case .accepted: Text("已送达 · 等待回复").font(.caption).foregroundStyle(.secondary)
                    case .needsRetry:
                        Text(outgoing.error ?? "发送结果待确认").font(.caption).foregroundStyle(.orange)
                        Button("重试并核对发送结果") { Task { await model.retry(outgoing.id) } }
                            .font(.caption).disabled(!model.isOnline || model.busy)
                    }
                } else if message.status == .failed {
                    Label("消息处理失败", systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.red)
                } else if message.status == .pending {
                    Text("等待处理…").font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(14)
            .background(message.role == .user ? Color.accentColor.opacity(0.12) : Color.secondary.opacity(0.08),
                        in: RoundedRectangle(cornerRadius: 14))
            if message.role != .user { Spacer(minLength: 35) }
        }
    }
}
