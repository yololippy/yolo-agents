import SwiftUI
import YoloAgentsSDK
#if os(macOS)
import AppKit
#elseif os(iOS)
import UIKit
#endif

// Resolve the long-standing property wrapper explicitly. The macOS 27 SDK also
// exports a same-named macro whose plugin is absent from Command Line Tools.
private typealias ViewState<Value> = SwiftUI.State<Value>

struct ConversationRenameRequest: Identifiable, Equatable {
    let id = UUID()
    let conversation: Conversation
    let initialTitle: String

    init(_ conversation: Conversation) {
        self.conversation = conversation
        initialTitle = conversation.title
    }
}

private func copyConversationID(_ id: UUID) {
    let value = id.uuidString.lowercased()
    #if os(macOS)
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(value, forType: .string)
    #elseif os(iOS)
    UIPasteboard.general.string = value
    #endif
}

public struct YoloAgentsRootView: View {
    @StateObject private var model: ChatModel
    @Environment(\.scenePhase) private var scenePhase
    private let features: [ChatFeature]
    private let messageAccessory: ChatMessageAccessory?
    private let composerConfiguration: AgentComposerConfiguration
    private let shellConfiguration: AgentShellConfiguration
    public init(identity: AgentAppIdentity, features: [ChatFeature] = [],
                messageAccessory: ChatMessageAccessory? = nil,
                composerConfiguration: AgentComposerConfiguration = .standard,
                shellConfiguration: AgentShellConfiguration = .conversationFirst) {
        self.features = features
        self.messageAccessory = messageAccessory
        self.composerConfiguration = composerConfiguration
        self.shellConfiguration = shellConfiguration
        _model = StateObject(wrappedValue: ChatModel(identity: identity))
    }
    public var body: some View {
        Group {
            if !model.started {
                ProgressView("正在打开…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.connection == nil {
                PairingView(model: model, isSettings: false)
            } else {
                ChatShell(model: model, features: features, messageAccessory: messageAccessory,
                          composerConfiguration: composerConfiguration,
                          configuration: shellConfiguration)
            }
        }
        .task { await model.start() }
        .onAppear { updateActivity(scenePhase) }
        .onChange(of: scenePhase, initial: true) { _, phase in updateActivity(phase) }
        .onDisappear { model.setActive(false) }
        .alert("操作未完成", isPresented: Binding(get: { model.errorMessage != nil }, set: {
            if !$0 { model.errorMessage = nil }
        })) {
            Button("知道了") { model.errorMessage = nil }
        } message: { Text(model.errorMessage ?? "") }
        #if os(macOS)
        .frame(minWidth: 760, minHeight: 560)
        #endif
    }
    private func updateActivity(_ phase: ScenePhase) {
        #if os(iOS)
        model.setActive(phase == .active)
        #else
        model.setActive(phase != .background)
        #endif
    }
}

private struct PairingView: View {
    @ObservedObject var model: ChatModel
    let isSettings: Bool
    var presentation: AgentContentPresentation = .modal
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
                    Label("连接 \(model.appName)", systemImage: "desktopcomputer.and.iphone")
                        .font(.title2.bold()).padding(.vertical, 8)
                    Text("在 Mac 服务端获取配对码，将聊天与历史同步到这台设备。")
                        .foregroundStyle(.secondary)
                }
                if let connection = model.connection, isSettings {
                    Section("当前连接") {
                        LabeledContent("服务", value: connection.endpoint.url.absoluteString)
                        LabeledContent("设备", value: connection.pairing.device.name)
                        LabeledContent("状态", value: model.connectionState.label)
                        LabeledContent("所属应用", value: model.appName)
                        Text("此客户端的配对和历史仅属于当前 Agent。")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                Section(isSettings ? "重新配对" : "服务地址") {
                    if let port = URL(string: model.defaultServiceAddress)?.port {
                        Text("\(model.appName) 服务默认端口 \(port)。手机请填写 Mac 可访问的地址。")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
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
                                if isSettings && presentation.showsDismissAction { dismiss() }
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
            .navigationTitle(isSettings ? "连接设置" : model.appName)
            .toolbar {
                if isSettings && presentation.showsDismissAction {
                    ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } }
                }
            }
            .confirmationDialog("清除本机配对和缓存？", isPresented: $confirmingDisconnect) {
                Button("清除本机记录", role: .destructive) {
                    model.disconnect()
                    if presentation.showsDismissAction { dismiss() }
                }
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
        model.defaultServiceAddress
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
    let features: [ChatFeature]
    let messageAccessory: ChatMessageAccessory?
    let composerConfiguration: AgentComposerConfiguration
    let configuration: AgentShellConfiguration
    @ViewState private var includeArchived = false
    @ViewState private var showSettings = false
    @ViewState private var showAccount = false
    @ViewState private var selectedFeature: ChatFeature?
    @ViewState private var compactNavigationState = AgentShellNavigation()
    @ViewState private var renameRequest: ConversationRenameRequest? = nil
    #if os(macOS)
    @ViewState private var drafts = ComposerDrafts()
    #endif

    private var sections: ConversationHomeSections {
        ConversationHomeSections(state: model.state, includeArchived: includeArchived)
    }

    private var conversations: [Conversation] {
        sections.primary + sections.recent + sections.archived
    }

    var body: some View {
        Group {
            #if os(iOS)
            compactContent
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    AgentActionDock(
                        selection: Binding(get: { compactNavigationState.destination },
                                           set: { if let destination = $0 { selectCompactDestination(destination) } }),
                        items: AgentShellNavigation.dockItems(features: features, configuration: configuration),
                        onSelect: { _ in }
                    )
                }
            #else
            splitNavigation
            #endif
        }
        .sheet(isPresented: $showSettings) { PairingView(model: model, isSettings: true) }
        .sheet(item: $selectedFeature) { feature in
            if let connection = model.connection { feature.content(connection) }
        }
        .sheet(isPresented: $showAccount) {
            if let connection = model.connection {
                ChatGPTAccountView(connection: connection, identity: model.identity) {
                    Task { await model.refreshAccountContext() }
                }
            }
        }
        .sheet(item: $renameRequest) { request in
            ConversationRenameView(request: request) { title in
                renameRequest = nil
                Task { await model.updateConversation(request.conversation, title: title) }
            } onCancel: {
                renameRequest = nil
            }
        }
    }

    #if os(iOS)
    @ViewBuilder
    private var compactContent: some View {
        switch compactNavigationState.destination {
        case .conversations:
            compactNavigation
        case .account:
            if let connection = model.connection {
                ChatGPTAccountView(connection: connection, identity: model.identity, presentation: .page) {
                    Task { await model.refreshAccountContext() }
                }
            }
        case .settings:
            PairingView(model: model, isSettings: true, presentation: .page)
        case .feature(let id):
            if let connection = model.connection, let feature = features.first(where: { $0.id == id }) {
                feature.content(connection)
            }
        }
    }

    private var compactNavigation: some View {
        NavigationStack(path: $compactNavigationState.conversationPath) {
            conversationHome
                .navigationDestination(for: UUID.self) { id in
                    ChatDetail(model: model, drafts: $compactNavigationState.drafts,
                               openSettings: { selectCompactDestination(.settings) },
                               openAccount: { selectCompactDestination(.account) }, features: features,
                               messageAccessory: messageAccessory,
                               composerConfiguration: composerConfiguration,
                               openFeature: { selectCompactDestination(.feature($0.id)) },
                               renameConversation: beginRename,
                               conversationArchived: { compactNavigationState.conversationPath.removeAll() })
                        .onAppear { model.selectedID = id }
                }
        }
    }

    private func selectCompactDestination(_ destination: AgentShellDestination) {
        compactNavigationState.select(destination, availableFeatureIDs: features.map(\.id))
    }

    private var conversationHome: some View {
        List {
            if !sections.primary.isEmpty {
                Section(configuration.primarySectionTitle) {
                    ForEach(sections.primary) { conversationRow($0) }
                }
            }
            if !sections.recent.isEmpty {
                Section(configuration.recentSectionTitle) {
                    ForEach(sections.recent) { conversationRow($0) }
                }
            }
            if !sections.archived.isEmpty {
                Section(configuration.archivedSectionTitle) {
                    ForEach(sections.archived) { conversationRow($0) }
                }
            }
            if conversations.isEmpty {
                ContentUnavailableView {
                    Label("还没有会话", systemImage: "bubble.left.and.bubble.right")
                } description: {
                    Text(model.isOnline ? "创建一个新会话开始聊天。" : "连接恢复后即可创建会话。")
                }
                .listRowBackground(Color.clear)
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle(configuration.conversationsTitle)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button { createAndOpenConversation() } label: {
                    Label("新会话", systemImage: "square.and.pencil")
                }
                .disabled(!model.isOnline || model.busy)
                .accessibilityIdentifier("chat.new-conversation")
                archiveToggle
            }
        }
    }

    private func conversationRow(_ conversation: Conversation) -> some View {
        Button {
            model.selectedID = conversation.id
            compactNavigationState.conversationPath.append(conversation.id)
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 6) {
                        Text(conversation.title).font(.body.weight(.medium)).lineLimit(1)
                        if model.isPrimaryConversation(conversation) {
                            Image(systemName: "pin.fill")
                                .font(.caption).foregroundStyle(.secondary)
                                .accessibilityLabel("主会话")
                        }
                    }
                    Text(conversation.updatedAt, style: .date)
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if ConversationActivity(state: model.state, conversationID: conversation.id).showsProgress {
                    ProgressView()
                        .controlSize(.small)
                        .accessibilityLabel("正在等待助手回复")
                        .accessibilityIdentifier("chat.conversation-progress.\(conversation.id.uuidString.lowercased())")
                } else {
                    Image(systemName: "chevron.right")
                        .font(.caption.bold()).foregroundStyle(.tertiary)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier(ConversationRowAccessibility.identifier(for: conversation.id))
        .contextMenu { conversationActions(conversation) }
    }

    private var archiveToggle: some View {
        Button { includeArchived.toggle() } label: {
            Label(includeArchived ? "隐藏已归档会话" : "显示已归档会话",
                  systemImage: includeArchived ? "archivebox.fill" : "archivebox")
        }
        .accessibilityIdentifier("chat.archived-toggle")
    }

    @ViewBuilder
    private func conversationActions(_ conversation: Conversation) -> some View {
        Button { copyConversationID(conversation.id) } label: {
            Label("复制会话 ID", systemImage: "doc.on.doc")
        }
        Button { beginRename(conversation) } label: {
            Label("重命名", systemImage: "pencil")
        }
        .disabled(!model.isOnline)
        Button {
            Task { await model.updateConversation(conversation, archived: !conversation.archived) }
        } label: {
            Label(conversation.archived ? "取消归档" : "归档",
                  systemImage: conversation.archived ? "archivebox.fill" : "archivebox")
        }
        .disabled(!model.isOnline || model.isPrimaryConversation(conversation))
    }

    private func createAndOpenConversation() {
        Task {
            await model.createConversation()
            guard let id = model.selectedID else { return }
            compactNavigationState.conversationPath.append(id)
        }
    }
    #else
    private var splitNavigation: some View {
        NavigationSplitView {
            VStack(spacing: 0) {
                List(selection: $model.selectedID) {
                    Button { Task { await model.createConversation() } } label: {
                        Label("新会话", systemImage: "square.and.pencil")
                    }.disabled(!model.isOnline || model.busy)
                    ForEach(conversations) { conversation in
                        NavigationLink(value: conversation.id) {
                            HStack {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(conversation.title).lineLimit(1)
                                    HStack {
                                        Text(conversation.updatedAt, style: .date)
                                        if model.isPrimaryConversation(conversation) {
                                            Label("主会话", systemImage: "pin.fill")
                                                .labelStyle(.titleAndIcon)
                                        }
                                        if conversation.archived { Image(systemName: "archivebox") }
                                    }
                                    .font(.caption).foregroundStyle(.secondary)
                                }
                                .padding(.vertical, 4)
                                Spacer()
                                if ConversationActivity(state: model.state, conversationID: conversation.id).showsProgress {
                                    ProgressView()
                                        .controlSize(.small)
                                        .accessibilityLabel("正在等待助手回复")
                                        .accessibilityIdentifier("chat.conversation-progress.\(conversation.id.uuidString.lowercased())")
                                }
                            }
                        }
                        .accessibilityIdentifier(ConversationRowAccessibility.identifier(for: conversation.id))
                        .contextMenu {
                            Button { copyConversationID(conversation.id) } label: {
                                Label("复制会话 ID", systemImage: "doc.on.doc")
                            }
                            Button("重命名") { beginRename(conversation) }
                                .disabled(!model.isOnline)
                            Button(conversation.archived ? "取消归档" : "归档") {
                                Task { await model.updateConversation(conversation, archived: !conversation.archived) }
                            }.disabled(!model.isOnline || model.isPrimaryConversation(conversation))
                        }
                    }
                }
                Toggle("显示归档", isOn: $includeArchived)
                    .toggleStyle(.switch).font(.caption).padding()
                ForEach(features) { feature in
                    Button { selectedFeature = feature } label: {
                        Label(feature.title, systemImage: feature.systemImage)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }.buttonStyle(.plain).padding(.horizontal).accessibilityIdentifier(feature.id)
                }
                Button { showAccount = true } label: {
                    Label("ChatGPT 账号", systemImage: "person.crop.circle")
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).padding(.horizontal).padding(.top, 8)
                Button { showSettings = true } label: {
                    Label(model.connectionState.label, systemImage: "gearshape")
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).padding()
            }
            .navigationTitle("会话")
        } detail: {
            ChatDetail(model: model, drafts: $drafts, openSettings: { showSettings = true },
                       openAccount: { showAccount = true }, features: features,
                       messageAccessory: messageAccessory, composerConfiguration: composerConfiguration,
                       openFeature: { selectedFeature = $0 }, renameConversation: beginRename,
                       conversationArchived: {})
        }
    }
    #endif

    private func beginRename(_ conversation: Conversation) {
        renameRequest = ConversationRenameRequest(conversation)
    }
}

private struct ConversationRenameView: View {
    let request: ConversationRenameRequest
    let onSave: (String) -> Void
    let onCancel: () -> Void
    @ViewState private var title: String
    @FocusState private var titleFocused: Bool

    init(request: ConversationRenameRequest, onSave: @escaping (String) -> Void,
         onCancel: @escaping () -> Void) {
        self.request = request
        self.onSave = onSave
        self.onCancel = onCancel
        _title = ViewState(initialValue: request.initialTitle)
    }

    private var normalizedTitle: String {
        title.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            Form {
                TextField("会话名称", text: $title)
                    .focused($titleFocused)
                    .onSubmit(save)
            }
            .formStyle(.grouped)
            .navigationTitle("重命名会话")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消", action: onCancel) }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存", action: save).disabled(normalizedTitle.isEmpty)
                }
            }
        }
        .onAppear { titleFocused = true }
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 180)
        #else
        .presentationDetents([.medium])
        #endif
    }

    private func save() {
        guard !normalizedTitle.isEmpty else { return }
        onSave(normalizedTitle)
    }
}

private struct ChatDetail: View {
    @ObservedObject var model: ChatModel
    @Binding var drafts: ComposerDrafts
    @Environment(\.dismiss) private var dismiss
    let openSettings: () -> Void
    let openAccount: () -> Void
    let features: [ChatFeature]
    let messageAccessory: ChatMessageAccessory?
    let composerConfiguration: AgentComposerConfiguration
    let openFeature: (ChatFeature) -> Void
    let renameConversation: (Conversation) -> Void
    let conversationArchived: () -> Void
    @ViewState private var composerDismissRequest = 0
    @ViewState private var scrollState = ConversationScrollState()
    private var composerAvailability: ComposerAvailability {
        ComposerAvailability(hasConversation: model.selectedID != nil,
                             archived: model.selectedConversation?.archived == true,
                             online: model.isOnline, busy: model.busy)
    }

    var body: some View {
        VStack(spacing: 0) {
            #if os(iOS)
            if !compactStatusProblems.isEmpty { compactStatusBanner }
            #else
            connectionBanner
            #endif
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
                #if os(macOS)
                modelSelector
                Divider()
                #endif
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 18) {
                            if model.canLoadEarlierMessages || model.loadingEarlierMessages {
                                Button {
                                    Task { await model.loadEarlierMessages() }
                                } label: {
                                    HStack(spacing: 8) {
                                        if model.loadingEarlierMessages {
                                            ProgressView().controlSize(.small)
                                        } else {
                                            Image(systemName: "arrow.up.circle")
                                        }
                                        Text(model.loadingEarlierMessages ? "正在加载更早消息…" : "加载更早消息")
                                    }
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.plain)
                                .disabled(model.loadingEarlierMessages)
                                .accessibilityIdentifier("chat.load-earlier-messages")
                                .task(id: model.earlierMessagesCursor) {
                                    await model.loadEarlierMessages()
                                }
                            }
                            ForEach(model.messages) { message in
                                MessageBubble(message: message, model: model, accessory: messageAccessory)
                                    .id(ConversationScrollTarget.message(message.id))
                            }
                            ForEach(model.partialReplies) { reply in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("助手").font(.caption.bold()).foregroundStyle(.secondary)
                                    if !reply.media.isEmpty {
                                        MessageMediaGallery(media: reply.media, model: model)
                                    }
                                    if !reply.content.isEmpty {
                                        AgentStreamingMarkdownView(reply.content)
                                    }
                                    if let error = reply.error {
                                        Label("回复失败：\(error.message)", systemImage: "exclamationmark.triangle")
                                            .font(.caption).foregroundStyle(.red)
                                    } else {
                                        Label(model.isOnline ? "正在回复…" : "等待重新同步…", systemImage: "ellipsis.bubble")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                                .padding(14).background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
                                .id(ConversationScrollTarget.partialReply(reply.id))
                            }
                            ForEach(model.awaitingReplies) { reply in
                                AwaitingReplyBubble(reply: reply, isOnline: model.isOnline)
                                    .id(ConversationScrollTarget.awaitingReply(reply.id))
                            }
                            if let id = model.selectedID {
                                Color.clear.frame(height: 1).id(ConversationScrollTarget.bottom(id))
                            }
                        }
                        .scrollTargetLayout()
                        .padding().frame(maxWidth: 900, alignment: .leading).frame(maxWidth: .infinity)
                    }
                    .defaultScrollAnchor(.bottom)
                    .scrollPosition(id: scrollPositionBinding, anchor: .bottom)
                    .scrollDismissesKeyboard(.interactively)
                    .simultaneousGesture(TapGesture().onEnded { composerDismissRequest += 1 })
                    .onChange(of: model.messages) { _, _ in contentChanged() }
                    .onChange(of: model.partialReplies) { _, _ in contentChanged() }
                    .onChange(of: model.awaitingReplies) { _, _ in contentChanged() }
                    .task(id: model.selectedID) {
                        guard let id = model.selectedID else { return }
                        scrollState.open(id)
                        await Task.yield()
                        guard model.selectedID == id else { return }
                        proxy.scrollTo(ConversationScrollTarget.bottom(id), anchor: .bottom)
                        await model.loadSelectedMessages()
                        guard model.selectedID == id else { return }
                        await Task.yield()
                        scrollState.open(id)
                        proxy.scrollTo(ConversationScrollTarget.bottom(id), anchor: .bottom)
                    }
                }
            }
        }
        .navigationTitle(model.selectedConversation?.title ?? model.appName)
        .toolbar {
            #if os(iOS)
            ToolbarItem(placement: .principal) { compactModelPicker }
            ToolbarItem(placement: .topBarTrailing) { compactConversationMenu }
            #else
            ToolbarItem(placement: .navigation) { newConversationButton }
            ToolbarItemGroup(placement: .primaryAction) {
                ForEach(features) { feature in
                    Button { openFeature(feature) } label: { Label(feature.title, systemImage: feature.systemImage) }
                        .accessibilityIdentifier(feature.id + ".toolbar")
                }
                Button(action: openAccount) { Label("ChatGPT 账号", systemImage: "person.crop.circle") }
                Button(action: openSettings) { Label("连接设置", systemImage: "gearshape") }
            }
            #endif
        }
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) {
                if let id = model.selectedID {
                    Divider()
                    ChatComposer(conversationID: id, text: Binding(
                        get: { drafts.text(for: id) }, set: { drafts.set($0, for: id) }),
                        images: Binding(
                            get: { drafts.images(for: id) },
                            set: { drafts.setImages($0, for: id) }),
                        isEditable: composerAvailability.canEdit,
                        canSend: composerAvailability.canSend,
                        online: model.isOnline, configuration: composerConfiguration,
                        dismissRequest: composerDismissRequest, onSend: { sendDraft(in: id) })
                        .id(id)
                }
            }.background(.bar)
        }
    }
    private var scrollPositionBinding: Binding<ConversationScrollTarget?> {
        Binding(
            get: { scrollState.target },
            set: { target in
                guard let id = model.selectedID else { return }
                scrollState.positionChanged(target, in: id)
            }
        )
    }
    private func contentChanged() {
        guard let id = model.selectedID else { return }
        scrollState.contentChanged(in: id)
    }
    private var newConversationButton: some View {
        Button { Task { await model.createConversation() } } label: {
            Label("新会话", systemImage: "square.and.pencil")
        }
        .keyboardShortcut("n", modifiers: .command)
        .accessibilityIdentifier("chat.new-conversation")
        .disabled(!model.isOnline || model.busy)
    }
    private func sendDraft(in id: UUID) {
        guard let submission = drafts.submission(for: id) else { return }
        Task {
            guard model.selectedID == id else { return }
            let accepted = await model.send(submission.content, images: submission.images)
            drafts.finish(submission, accepted: accepted)
        }
    }
    #if os(iOS)
    private var compactModelPicker: some View {
        Menu {
            Button {
                Task { await model.changeModel(.serverDefault) }
            } label: {
                if model.selectedConversation?.model == nil {
                    Label("使用服务默认模型", systemImage: "checkmark")
                } else {
                    Text("使用服务默认模型")
                }
            }
            ForEach(model.modelCatalog?.models ?? []) { option in
                Button {
                    Task { await model.changeModel(.model(option.id)) }
                } label: {
                    if model.selectedConversation?.model == option.id {
                        Label(option.name, systemImage: "checkmark")
                    } else {
                        Text(option.name)
                    }
                }
            }
            Divider()
            Button("刷新模型列表") { Task { await model.refreshModels() } }
                .disabled(model.modelCatalogLoading)
        } label: {
            VStack(spacing: 1) {
                Text(model.selectedConversation?.title ?? model.appName)
                    .font(.headline).lineLimit(1)
                HStack(spacing: 3) {
                    Text(model.selectedModelName)
                    Image(systemName: "chevron.down").font(.caption2)
                }
                .font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: 210)
        }
        .accessibilityLabel("会话与模型")
        .accessibilityIdentifier("chat.model-picker")
        .disabled(!model.isOnline || model.busy || model.selectedConversation?.archived == true)
    }

    private var compactConversationMenu: some View {
        Menu {
            if let conversation = model.selectedConversation {
                Button { renameConversation(conversation) } label: {
                    Label("重命名", systemImage: "pencil")
                }
                .disabled(!model.isOnline)
                Button { copyConversationID(conversation.id) } label: {
                    Label("复制会话 ID", systemImage: "doc.on.doc")
                }
                Button { updateArchiveState(conversation) } label: {
                    Label(conversation.archived ? "取消归档" : "归档",
                          systemImage: conversation.archived ? "archivebox.fill" : "archivebox")
                }
                .disabled(!model.isOnline || model.isPrimaryConversation(conversation))
            }
        } label: {
            Label("会话操作", systemImage: "ellipsis")
        }
        .accessibilityIdentifier("chat.conversation-menu")
    }

    private func updateArchiveState(_ conversation: Conversation) {
        let shouldArchive = !conversation.archived
        Task {
            await model.updateConversation(conversation, archived: shouldArchive)
            guard shouldArchive,
                  model.state.conversations.first(where: { $0.id == conversation.id })?.archived == true else { return }
            conversationArchived()
            dismiss()
        }
    }

    private var compactStatusProblems: [ServiceStatusPresentation] {
        var problems: [ServiceStatusPresentation] = []
        if model.connectionState != .online {
            problems.append(ServiceStatusPresentation(model.connectionState))
        }
        if model.modelConnectionState != .available && model.modelConnectionState != .disabled {
            problems.append(ServiceStatusPresentation(model.modelConnectionState))
        }
        problems.append(contentsOf: model.serviceComponents.filter { $0.state != "ready" }.map(ServiceStatusPresentation.init))
        return problems
    }

    private var compactStatusBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(compactStatusProblems.enumerated()), id: \.offset) { _, problem in
                    Text("\(problem.title)：\(problem.value)").font(.caption).lineLimit(1)
                }
            }
            Spacer(minLength: 4)
            if model.connectionState == .authorizationRequired {
                Button("重新配对", action: openSettings).font(.caption)
            } else if !model.isOnline {
                Button("重连") { model.reconnect() }.font(.caption)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(.orange.opacity(0.1))
        .accessibilityIdentifier("chat.status-problems")
    }
    #endif

    private var modelSelector: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Menu {
                    Button {
                        Task { await model.changeModel(.serverDefault) }
                    } label: {
                        if model.selectedConversation?.model == nil { Label("使用服务默认模型", systemImage: "checkmark") }
                        else { Text("使用服务默认模型") }
                    }
                    ForEach(model.modelCatalog?.models ?? []) { option in
                        Button {
                            Task { await model.changeModel(.model(option.id)) }
                        } label: {
                            if model.selectedConversation?.model == option.id { Label(option.name, systemImage: "checkmark") }
                            else { Text(option.name) }
                        }
                    }
                    Divider()
                    Button("刷新模型列表") { Task { await model.refreshModels() } }
                        .disabled(model.modelCatalogLoading)
                } label: {
                    Label(model.selectedModelName, systemImage: "chevron.down")
                }
                .accessibilityLabel("会话模型")
                .accessibilityIdentifier("chat.model-picker")
                .disabled(!model.isOnline || model.busy || model.selectedConversation?.archived == true)
                Spacer()
                if model.modelCatalogLoading { ProgressView().controlSize(.small) }
                if model.modelCatalog?.auth?.needsLogin == true {
                    Text("请在此 Agent 的 Mac 客户端完成 ChatGPT 登录").font(.caption).foregroundStyle(.orange)
                }
            }
            if let error = model.modelCatalogError {
                Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                Button("重新加载模型") { Task { await model.refreshModels() } }.font(.caption)
            }
        }.padding(.horizontal).padding(.vertical, 8)
    }
    private var connectionBanner: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top, spacing: 7) {
                ServiceStatusBadge(presentation: ServiceStatusPresentation(model.connectionState))
                    .frame(maxWidth: .infinity, alignment: .leading)
                ServiceStatusBadge(presentation: ServiceStatusPresentation(model.modelConnectionState))
                    .frame(maxWidth: .infinity, alignment: .leading)
                ForEach(model.serviceComponents) { component in
                    ServiceStatusBadge(presentation: ServiceStatusPresentation(component))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }

            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if case .offline(let reason) = model.connectionState {
                    Text(reason).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                } else if let health = model.health, health.agentMode == "demo" {
                    Text("演示模式 · 未调用真实模型或外部服务")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                if model.connectionState == .authorizationRequired {
                    Button("重新配对", action: openSettings).font(.caption)
                } else if !model.isOnline {
                    Button("重连") { model.reconnect() }.font(.caption)
                }
            }
        }
        .padding(.horizontal).padding(.vertical, 8).background(.bar)
    }
}

enum ServiceStatusTone: Equatable {
    case positive, warning, negative, neutral
}

struct ServiceStatusPresentation: Equatable {
    let title: String
    let value: String
    let tone: ServiceStatusTone
    let accessibilityIdentifier: String

    init(title: String, value: String, tone: ServiceStatusTone, accessibilityIdentifier: String) {
        self.title = title
        self.value = value
        self.tone = tone
        self.accessibilityIdentifier = accessibilityIdentifier
    }

    init(_ state: ConnectionState) {
        title = "Mac 服务"
        accessibilityIdentifier = "status.mac-service"
        switch state {
        case .online: value = "已连接"; tone = .positive
        case .connecting: value = "连接中"; tone = .warning
        case .offline: value = "已断开"; tone = .negative
        case .authorizationRequired: value = "需重新配对"; tone = .negative
        case .disconnected: value = "未连接"; tone = .neutral
        }
    }

    init(_ state: ModelConnectionState) {
        title = "AI 模型"
        accessibilityIdentifier = "status.ai-model"
        switch state {
        case .checking: value = "检查中"; tone = .warning
        case .available: value = "可用"; tone = .positive
        case .recovering: value = "待重试"; tone = .warning
        case .unavailable: value = "不可用"; tone = .negative
        case .disabled: value = "未启用"; tone = .neutral
        }
    }

    init(_ component: ServiceComponentHealth) {
        title = component.label
        value = component.detail ?? {
            switch component.state {
            case "ready": "可用"
            case "checking": "检查中"
            case "unavailable": "不可用"
            default: component.state
            }
        }()
        switch component.state {
        case "ready": tone = .positive
        case "checking": tone = .warning
        case "unavailable": tone = .negative
        default: tone = .neutral
        }
        accessibilityIdentifier = "status.component.\(component.id)"
    }
}

private struct ServiceStatusBadge: View {
    let presentation: ServiceStatusPresentation

    private var color: Color {
        switch presentation.tone {
        case .positive: .green
        case .warning: .orange
        case .negative: .red
        case .neutral: .secondary
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            Circle().fill(color).frame(width: 7, height: 7).padding(.top, 4)
            VStack(alignment: .leading, spacing: 1) {
                Text(presentation.title).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                Text(presentation.value).font(.caption.bold()).lineLimit(1)
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 6)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier(presentation.accessibilityIdentifier)
    }
}

struct AwaitingReplyPresentation: Equatable {
    let status: String
    let showsProgress: Bool
    let accessibilityIdentifier: String

    init(reply: AwaitingReply, isOnline: Bool) {
        status = isOnline ? "正在思考…" : "等待重新同步…"
        showsProgress = isOnline
        accessibilityIdentifier = "chat.awaiting-reply.\(reply.id.uuidString.lowercased())"
    }
}

private struct AwaitingReplyBubble: View {
    let reply: AwaitingReply
    let isOnline: Bool
    private var presentation: AwaitingReplyPresentation {
        AwaitingReplyPresentation(reply: reply, isOnline: isOnline)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("助手").font(.caption.bold()).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                if presentation.showsProgress {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "arrow.triangle.2.circlepath")
                        .foregroundStyle(.secondary)
                }
                Text(presentation.status).foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
        .accessibilityIdentifier(presentation.accessibilityIdentifier)
    }
}

private struct MessageBubble: View {
    let message: Message
    @ObservedObject var model: ChatModel
    let accessory: ChatMessageAccessory?
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
                if !message.media.isEmpty {
                    MessageMediaGallery(media: message.media, model: model)
                }
                if !message.content.isEmpty {
                    if message.role == .assistant {
                        AgentMarkdownView(message.content)
                    } else {
                        Text(message.content)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                if let accessory, let connection = model.connection {
                    accessory.content(message, connection)
                }
                if let outgoing {
                    switch outgoing.status {
                    case .submitting: Text("正在发送…").font(.caption).foregroundStyle(.secondary)
                    case .accepted: Text("已送达").font(.caption).foregroundStyle(.secondary)
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
