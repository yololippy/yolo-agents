import SwiftUI
import YoloAgentsSDK
import YoloAgentsUI

private typealias TradeViewState<Value> = SwiftUI.State<Value>

struct TradeView: View {
    let connection: PairedConnection
    let presentation: AgentContentPresentation
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model: TradeModel
    @StateObject private var orders: PaperOrdersModel
    @TradeViewState private var page: TradePage = .positions
    @TradeViewState private var query = ""
    @TradeViewState private var ticket: TradeTicketSelection?
    @TradeViewState private var showDevices = false
    init(connection: PairedConnection, presentation: AgentContentPresentation = .modal) {
        self.connection = connection; self.presentation = presentation
        _model = StateObject(wrappedValue: TradeModel(connection: connection))
        _orders = StateObject(wrappedValue: PaperOrdersModel(connection: connection))
    }
    var body: some View {
        VStack(spacing: 0) {
            if presentation.showsDismissAction {
                HStack { Spacer(); Button("完成") { dismiss() }.accessibilityIdentifier("trade.close") }.padding([.top, .horizontal])
            }
            Picker("交易", selection: $page) {
                ForEach(TradePage.allCases, id: \.self) { Text($0.title).tag($0) }
            }.pickerStyle(.segmented).padding().accessibilityIdentifier("trade.pages")
            if page == .orders { PaperOrdersView(connection: connection, presentation: .page) }
            else {
                NavigationStack {
                    List {
                        if let error = model.error { Text(error).foregroundStyle(.red) }
                        if page == .search {
                            Section {
                                TextField("股票代码或名称", text: $query).accessibilityIdentifier("trade.search")
                                    .onChange(of: query) { _, value in model.search(value) }
                                if model.isSearching { ProgressView("搜索中…") }
                                ForEach(model.results) { instrument in instrumentRow(instrument) }
                                if !model.isSearching && !query.isEmpty && model.results.isEmpty && model.error == nil { Text("没有匹配的股票") }
                            }
                        } else {
                            Section("账户持仓") {
                                if model.positions.isEmpty { Text("暂无持仓数据").foregroundStyle(.secondary) }
                                ForEach(model.positions) { position in
                                    VStack(alignment: .leading, spacing: 8) {
                                        instrumentRow(position.contract)
                                        LabeledContent("数量", value: position.quantity ?? "未提供")
                                        LabeledContent("均价", value: position.averageCost ?? "未提供")
                                        LabeledContent("当前价", value: position.marketPrice ?? "未提供")
                                        LabeledContent("市值", value: position.marketValue ?? "未提供")
                                        LabeledContent("未实现盈亏", value: position.unrealizedPNL ?? "未提供")
                                        LabeledContent("更新时间", value: (position.asOf ?? model.positionsAsOf)?.formatted() ?? "未提供")
                                    }.font(.callout).padding(.vertical, 6)
                                }
                            }
                        }
                        if let selected = model.selected {
                            Section("所选股票 · \(selected.symbol)") {
                                if let quote = model.quote { TradeQuoteView(quote: quote) }
                                else { Text(model.quoteError ?? "正在获取参考行情…").foregroundStyle(.secondary) }
                                if let contract = selected.quickContract {
                                    if orders.canManage {
                                        HStack {
                                            Button("买入") { ticket = TradeTicketSelection(contract: contract, side: .buy, quote: model.quote) }
                                            Button("卖出") { ticket = TradeTicketSelection(contract: contract, side: .sell, quote: model.quote) }
                                        }.buttonStyle(.bordered).disabled(orders.isWriting)
                                    } else {
                                        Text("此设备未获交易授权。请在 Mac 的交易页面打开“设备授权”，授权此已配对 iPhone 的 Paper 交易。")
                                            .foregroundStyle(.orange).accessibilityIdentifier("trade.authorization-required")
                                    }
                                } else { Text("此资产仅供查看；快速交易仅支持 STK / SMART / USD 且有明确主交易所的合约。") }
                            }
                        }
                    }
                    .navigationTitle("交易")
                    .toolbar {
                        ToolbarItem(placement: .primaryAction) {
                            Button("刷新", systemImage: "arrow.clockwise") { Task { await refresh() } }
                        }
                        if orders.isDeviceManager {
                            ToolbarItem { Button("设备授权", systemImage: "iphone.and.arrow.forward") {
                                showDevices = true
                            } }
                        }
                    }
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 760, minHeight: 600)
        #endif
        .task { await refresh() }
        .sheet(item: $ticket, onDismiss: { Task { await orders.refresh() } }) { value in
            PaperOrdersView(connection: connection, contract: value.contract, side: value.side, quote: value.quote)
        }
        .sheet(isPresented: $showDevices) { deviceSheet }
    }
    private func refresh() async { await orders.refresh(); await model.refreshPositions() }
    private func instrumentRow(_ instrument: TradeInstrument) -> some View {
        Button { Task { await model.select(instrument) } } label: {
            VStack(alignment: .leading, spacing: 4) {
                Text("\(instrument.symbol) · \(instrument.name ?? "名称未提供")").font(.headline)
                Text("\(instrument.secType) / \(instrument.exchange ?? "交易所未提供") / \(instrument.currency) · \(instrument.primaryExchange ?? "主交易所未提供") · conId \(instrument.conID)")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }.buttonStyle(.plain).accessibilityIdentifier("trade.instrument.\(instrument.conID)")
    }
    private var deviceSheet: some View {
        NavigationStack {
            List {
                if let error = orders.deviceAuthorizationError ?? orders.actionError { Text(error).foregroundStyle(.red) }
                ForEach(orders.tradingDevices) { device in
                    VStack(alignment: .leading) {
                        Text(device.name)
                        Text(device.permitsPaper ? "已授权 Paper 交易" : "未授权 Paper 交易").font(.caption)
                        Button(device.permitsPaper ? "撤销 Paper 交易授权" : "授权 Paper 交易", role: device.permitsPaper ? .destructive : nil) {
                            Task { await orders.setDeviceAuthorization(device.deviceID, authorized: !device.permitsPaper) }
                        }.disabled(orders.isWriting || device.revokedAt != nil)
                    }
                }
                if orders.tradingDevices.isEmpty { Text("没有可授权的设备，或服务端尚未提供设备授权接口。") }
            }
            .navigationTitle("Paper 交易设备授权")
            .toolbar { Button("完成") { showDevices = false } }
            .task { try? await orders.refreshDeviceAuthorization() }
        }
        #if os(macOS)
        .frame(minWidth: 480, minHeight: 320)
        #endif
    }
}
private struct TradeTicketSelection: Identifiable {
    let id = UUID()
    let contract: PaperStockContract
    let side: PaperOrderSide
    let quote: TradeQuote?
}
struct TradeQuoteView: View {
    let quote: TradeQuote
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            LabeledContent("Bid / Ask / Last", value: "\(quote.bid ?? "未提供") / \(quote.ask ?? "未提供") / \(quote.last ?? "未提供")")
            LabeledContent("行情类型", value: quote.dataType)
            LabeledContent("行情时间", value: quote.asOf?.formatted() ?? "未提供")
            Text("行情仅供参考。限价必须手动填写。").font(.caption).foregroundStyle(.secondary)
        }.accessibilityIdentifier("trade.quote")
    }
}
