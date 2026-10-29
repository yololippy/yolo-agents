import SwiftUI
import YoloAgentsSDK

private typealias OrderViewState<Value> = SwiftUI.State<Value>

struct PaperOrdersView: View {
    @StateObject private var model: PaperOrdersModel
    private let proposal: PaperOrderProposal?
    private let selectedOrderID: UUID?
    @Environment(\.dismiss) private var dismiss
    #if os(macOS)
    @OrderViewState private var reviewing = false
    #endif

    init(connection: PairedConnection, proposal: PaperOrderProposal? = nil, selectedOrderID: UUID? = nil) {
        self.proposal = proposal
        self.selectedOrderID = selectedOrderID
        _model = StateObject(wrappedValue: PaperOrdersModel(connection: connection))
    }

    var body: some View {
        NavigationSplitView {
            List(selection: $model.selectedID) {
                #if os(macOS)
                if model.canManage {
                    Button { model.newTicket() } label: { Label("新建 Paper 草稿", systemImage: "plus") }
                        .disabled(model.isWriting).accessibilityIdentifier("orders.new-draft")
                }
                #endif
                ForEach(model.orders) { order in
                    NavigationLink(value: order.id) {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("\(order.side.rawValue) \(order.contract.symbol)").font(.headline)
                            Text("\(order.quantity) 股 · 限价 \(order.limitPrice) USD").font(.caption)
                            Text(order.state.label).font(.caption).foregroundStyle(order.state.isUnresolved ? .orange : .secondary)
                        }.padding(.vertical, 4)
                    }.accessibilityIdentifier("orders.row.\(order.id.uuidString)")
                }
            }
            .navigationTitle("Paper 订单")
        } detail: {
            VStack(spacing: 0) {
                notices
                if let order = model.currentOrder {
                    orderDetail(order)
                } else if model.selectedID != nil {
                    ContentUnavailableView {
                        Label("正在读取已有 Paper 草稿", systemImage: "doc.text.magnifyingglass")
                    } description: {
                        Text("只会读取服务端已有订单；读取失败时不会创建替代草稿。")
                    }.accessibilityIdentifier("orders.existing-draft-loading")
                } else {
                    #if os(macOS)
                    if model.canManage { PaperOrderTicket(model: model, proposal: proposal) }
                    else { readOnlyPlaceholder }
                    #else
                    readOnlyPlaceholder
                    #endif
                }
            }
            .navigationTitle(model.currentOrder.map { "\($0.contract.symbol) · Paper" } ?? "Paper 订单")
        }
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("完成") { dismiss() }.disabled(model.isWriting)
                    .accessibilityIdentifier("orders.close")
            }
            ToolbarItem(placement: .primaryAction) {
                Button { Task { await model.refresh() } } label: {
                    Label("刷新订单", systemImage: "arrow.clockwise")
                }.disabled(model.isRefreshing || model.isWriting).accessibilityIdentifier("orders.refresh")
            }
        }
        .task {
            if let selectedOrderID { model.selectedID = selectedOrderID }
            await model.refresh()
        }
        .task(id: model.selectedID) { await model.loadSelected() }
        #if os(macOS)
        .frame(minWidth: 860, idealWidth: 1000, minHeight: 660)
        .confirmationDialog(model.review?.title ?? "核对 Paper 订单", isPresented: $reviewing,
                            titleVisibility: .visible) {
            if let review = model.review {
                Button(review.buttonTitle, role: review.kind == "cancel" ? .destructive : nil) {
                    Task { await model.performReviewedAction(review.id) }
                }.accessibilityIdentifier("orders.review.execute.\(review.kind)")
            }
            Button("返回", role: .cancel) { model.discardReview() }
        } message: { Text(model.review?.summary ?? "") }
        #endif
    }

    private var readOnlyPlaceholder: some View {
        ContentUnavailableView {
            Label("只读订单历史", systemImage: "list.bullet.rectangle")
        } description: { Text("选择订单查看状态、预览和事件。Paper 订单操作请在有管理权限的 Mac 上进行。") }
            .accessibilityIdentifier("orders.read-only")
    }

    private var notices: some View {
        VStack(alignment: .leading, spacing: 8) {
            #if os(iOS)
            Label("此设备只读 · 状态以 Mac 服务端为准", systemImage: "eye")
                .accessibilityIdentifier("orders.read-only")
            #endif
            if model.isRefreshing || model.isWriting {
                HStack { ProgressView().controlSize(.small); Text(model.isWriting ? "正在处理此项操作…" : "正在读取订单…") }
            }
            if let error = model.readError { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
            if let error = model.actionError { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.red) }
            if let message = model.uncertainty {
                Label(message, systemImage: "questionmark.diamond").foregroundStyle(.orange)
                    .accessibilityIdentifier("orders.uncertain")
            }
        }.font(.callout).padding().frame(maxWidth: .infinity, alignment: .leading)
    }

    private func orderDetail(_ order: PaperOrder) -> some View {
        Form {
            orderFlow(order)
            Section("订单状态") {
                LabeledContent("环境", value: "Paper 模拟账户")
                LabeledContent("状态", value: order.state.label).accessibilityIdentifier("orders.state")
                LabeledContent("订单 ID") { Text(order.id.uuidString).textSelection(.enabled) }
                LabeledContent("已成交", value: "\(order.filledQuantity) / \(order.quantity) 股")
                if let price = order.averageFillPrice { LabeledContent("成交均价", value: "\(price) USD") }
                if let code = order.errorCode { LabeledContent("服务错误", value: code) }
                if order.state.isUnresolved {
                    Text(order.state == .cancelPending ? "撤单请求正在核对，尚未确认撤销。请刷新查看最终状态。" : "执行结果尚未确定。请刷新并核对成交与订单状态。")
                        .foregroundStyle(.orange).accessibilityIdentifier("orders.unresolved-state")
                }
            }
            Section("订单内容") {
                LabeledContent("股票", value: order.contract.symbol)
                LabeledContent("方向", value: order.side == .buy ? "买入 BUY" : "卖出 SELL")
                LabeledContent("数量", value: "\(order.quantity) 股")
                LabeledContent("限价", value: "\(order.limitPrice) USD")
                LabeledContent("合约", value: "STK / SMART / USD")
                LabeledContent("类型与有效期", value: "LMT / DAY")
                LabeledContent("版本", value: String(order.version))
            }
            if let preview = order.preview {
                Section("What-if 预览") {
                    LabeledContent("合约确认", value: "\(preview.qualifiedContract.symbol) · conId \(preview.qualifiedContract.conID)")
                    LabeledContent("初始保证金变动", value: "\(preview.whatIf.initialMarginChange) \(preview.whatIf.currency)")
                    LabeledContent("维持保证金变动", value: "\(preview.whatIf.maintenanceMarginChange) \(preview.whatIf.currency)")
                    LabeledContent("佣金估算", value: "\(preview.whatIf.commission) \(preview.whatIf.currency)")
                    LabeledContent("预览有效至") { Text(preview.expiresAt.date, format: .dateTime) }
                    Text("保证金与佣金为服务端返回的预估，最终结果以成交记录为准。")
                        .font(.footnote).foregroundStyle(.secondary)
                }.accessibilityIdentifier("orders.preview-details")
            }
            #if os(macOS)
            if model.canManage { macActions(order) }
            #endif
            Section("事件历史") {
                if let detail = model.detail, detail.order.id == order.id {
                    ForEach(detail.events) { event in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(event.state?.label ?? event.type)
                            Text(event.type).font(.caption).foregroundStyle(.secondary)
                            if let code = event.errorCode { Text(code).font(.caption).foregroundStyle(.orange) }
                            Text(event.createdAt, format: .dateTime).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if detail.events.isEmpty { Text("暂无事件").foregroundStyle(.secondary) }
                } else { Text("刷新以读取最新事件。").foregroundStyle(.secondary) }
            }
        }.formStyle(.grouped)
    }

    private func orderFlow(_ order: PaperOrder) -> some View {
        Section("明确确认流程") {
            flowRow(1, "创建草稿", complete: true, current: order.state == .draft)
            flowRow(2, "获取 What-if", complete: order.preview != nil, current: [.draft, .previewing].contains(order.state))
            flowRow(3, "确认 What-if 并授权提交", complete: order.confirmation != nil, current: order.state == .proposed)
            flowRow(4, "提交 Paper 订单", complete: ![.draft, .previewing, .proposed, .confirmed].contains(order.state), current: order.state == .confirmed)
            Text("Mac 会先获取 What-if；你明确确认一次后，应用会连续完成确认与 Paper 提交。聊天模型无法自行执行。")
                .font(.footnote).foregroundStyle(.secondary)
        }.accessibilityIdentifier("orders.confirmation-flow")
    }

    @ViewBuilder
    private func flowRow(_ number: Int, _ title: String, complete: Bool, current: Bool) -> some View {
        Label {
            HStack {
                Text("\(number). \(title)")
                Spacer()
                Text(complete ? "已完成" : current ? "当前步骤" : "等待")
                    .font(.caption).foregroundStyle(current ? .orange : .secondary)
            }
        } icon: {
            Image(systemName: complete ? "checkmark.circle.fill" : current ? "circle.inset.filled" : "circle")
                .foregroundStyle(complete ? .green : current ? .orange : .secondary)
        }
    }

    #if os(macOS)
    private func macActions(_ order: PaperOrder) -> some View {
        Section("分步操作") {
            if !model.executionReady {
                Text("Paper 执行条件尚未就绪。刷新后，只有服务端账户、连接和权限检查全部通过才能继续。")
                    .foregroundStyle(.orange).accessibilityIdentifier("orders.execution-unavailable")
                ForEach(model.broker?.blockers ?? [], id: \.code) { blocker in
                    Text(blocker.message).font(.caption).foregroundStyle(.secondary)
                }
            }
            TimelineView(.periodic(from: .now, by: 1)) { context in
                VStack(alignment: .leading, spacing: 12) {
                    if [.draft, .proposed].contains(order.state) {
                        Button(order.state == .draft ? "获取 What-if 并继续提交" : "核对 What-if 并提交") {
                            Task {
                                await model.prepareCompleteSubmission()
                                reviewing = model.review != nil
                            }
                        }
                        .disabled(order.state == .draft ? !model.canPreview(order) : !model.canConfirm(order, now: context.date))
                        .accessibilityIdentifier("orders.complete-submit")
                    }
                    if order.state == .confirmed {
                        Button("提交此 Paper 订单") {
                            model.reviewSubmission(); reviewing = model.review != nil
                        }.disabled(!model.canSubmit(order, now: context.date)).accessibilityIdentifier("orders.submit")
                        if !model.canSubmit(order, now: context.date) && model.executionReady && !model.isWriting {
                            Text("提交仅接受本次打开界面后明确确认且未过期的预览。重新打开或已过期时请新建草稿。")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                    if order.state.canCancel {
                        Button("撤销此单", role: .destructive) {
                            model.reviewCancellation(); reviewing = model.review != nil
                        }.disabled(!model.canCancel(order)).accessibilityIdentifier("orders.cancel")
                    }
                    if let expiry = order.preview?.expiresAt, expiry.date <= context.date,
                       [.proposed, .confirmed].contains(order.state) {
                        Text("预览已过期，请新建草稿。").foregroundStyle(.orange)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
    #endif
}

#if os(macOS)
private struct PaperOrderTicket: View {
    @ObservedObject var model: PaperOrdersModel
    let proposal: PaperOrderProposal?
    @OrderViewState private var symbol = ""
    @OrderViewState private var side: PaperOrderSide = .buy
    @OrderViewState private var quantity = ""
    @OrderViewState private var limitPrice = ""

    init(model: PaperOrdersModel, proposal: PaperOrderProposal?) {
        self.model = model
        self.proposal = proposal
        _symbol = OrderViewState(initialValue: proposal?.symbol ?? "")
        _side = OrderViewState(initialValue: proposal?.side ?? .buy)
        _quantity = OrderViewState(initialValue: proposal?.quantity.map(String.init) ?? "")
        _limitPrice = OrderViewState(initialValue: proposal?.limitPrice ?? "")
    }

    private var issues: [String] {
        var values: [String] = []
        if (try? PaperStockContract(symbol: symbol)) == nil { values.append(symbol.isEmpty ? "请填写股票代码" : "股票代码无效") }
        if quantity.range(of: #"^[1-9][0-9]{0,5}$"#, options: .regularExpression) == nil || Int(quantity).map({ !(1...100_000).contains($0) }) == true {
            values.append(quantity.isEmpty ? "请填写股数" : "股数必须是 1–100000 的整数")
        }
        if !PaperOrder.validLimitPrice(limitPrice) { values.append(limitPrice.isEmpty ? "请填写限价" : "限价格式无效") }
        return values
    }

    var body: some View {
        Form {
            Section("明确确认流程") {
                Label("1. 创建草稿 · 当前步骤", systemImage: "circle.inset.filled").foregroundStyle(.orange)
                Label("2. 获取 What-if", systemImage: "circle").foregroundStyle(.secondary)
                Label("3. 确认 What-if 并授权提交", systemImage: "circle").foregroundStyle(.secondary)
                Label("4. 提交 Paper 订单", systemImage: "circle").foregroundStyle(.secondary)
                Text("聊天提案不会自动创建、确认或提交订单。")
                    .font(.footnote).foregroundStyle(.secondary)
            }.accessibilityIdentifier("orders.confirmation-flow")
            Section("创建 Paper 草稿") {
                if proposal != nil {
                    Label("已从聊天提案带入；请逐项核对", systemImage: "text.bubble")
                        .foregroundStyle(.orange).accessibilityIdentifier("orders.ticket.from-proposal")
                }
                LabeledContent("账户", value: model.accountSelection == nil ? "服务端尚未提供 Paper 配置" : "服务端已配置的 Paper 账户")
                TextField("股票代码，例如 AAPL", text: $symbol).accessibilityIdentifier("orders.ticket.symbol")
                Picker("方向", selection: $side) {
                    Text("买入 BUY").tag(PaperOrderSide.buy)
                    Text("卖出 SELL").tag(PaperOrderSide.sell)
                }.accessibilityIdentifier("orders.ticket.side")
                TextField("股数（正整数）", text: $quantity).accessibilityIdentifier("orders.ticket.quantity")
                TextField("每股限价 USD", text: $limitPrice).accessibilityIdentifier("orders.ticket.limit-price")
                Text("美股 STK / SMART / USD · 限价 LMT · 当日 DAY")
                    .font(.footnote).foregroundStyle(.secondary)
                if !issues.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(issues, id: \.self) { Label($0, systemImage: "exclamationmark.circle") }
                    }.font(.caption).foregroundStyle(.orange).accessibilityIdentifier("orders.ticket.issues")
                }
                Button("创建草稿") {
                    Task { await model.createDraft(symbol: symbol, side: side, quantity: quantity, limitPrice: limitPrice) }
                }.disabled(!model.canCreateDraft || !issues.isEmpty).accessibilityIdentifier("orders.ticket.create")
                Text("创建后获取 What-if；核对合约、保证金和佣金并确认一次，应用会继续提交。")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }.formStyle(.grouped)
    }
}
#endif
