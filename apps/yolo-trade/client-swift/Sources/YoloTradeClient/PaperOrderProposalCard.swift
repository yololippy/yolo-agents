import SwiftUI
import YoloAgentsSDK

private typealias ProposalViewState<Value> = SwiftUI.State<Value>

struct PaperOrderProposalCard: View {
    let message: Message
    let connection: PairedConnection
    #if os(macOS)
    @ProposalViewState private var showingPaperOrders = false
    #endif

    private var proposal: PaperOrderProposal? {
        guard message.role == .assistant, message.status == .completed else { return nil }
        return PaperOrderProposal(messageContent: message.content)
    }

    var body: some View {
        if let proposal {
            VStack(alignment: .leading, spacing: 10) {
                Label(proposal.kind.label, systemImage: "doc.text.magnifyingglass")
                    .font(.headline)
                proposalSummary(proposal)
                if !proposal.issues.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("消息中的订单信息需要补充或修正：").font(.caption.bold())
                        ForEach(proposal.issues, id: \.self) { issue in
                            Label(issue, systemImage: "exclamationmark.circle")
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .accessibilityIdentifier("paper-proposal.issues")
                }
                Text(proposal.kind == .proposal
                     ? "模型输出仅是提案，不能确认或提交订单。"
                     : "这是服务端非执行草稿；Mac 获取 What-if 后会请你确认一次，确认后连续提交。")
                    .font(.caption).foregroundStyle(.secondary)
                #if os(macOS)
                Group {
                    switch proposal.destination {
                    case .createDraft:
                        Button(proposal.isComplete ? "在 Paper 订单中创建草稿" : "打开草稿并补全字段") {
                            showingPaperOrders = true
                        }
                        .buttonStyle(.borderedProminent)
                        .accessibilityIdentifier("paper-proposal.open-draft")
                    case .existingDraft:
                        Button("核对并提交已有 Paper 草稿") { showingPaperOrders = true }
                            .buttonStyle(.borderedProminent)
                            .accessibilityIdentifier("paper-draft.open-existing")
                    case .unavailable:
                        Label("无法定位已有草稿；不会创建替代草稿", systemImage: "exclamationmark.triangle")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                .sheet(isPresented: $showingPaperOrders) {
                    switch proposal.destination {
                    case .createDraft:
                        PaperOrdersView(connection: connection, proposal: proposal)
                    case .existingDraft(let orderID):
                        PaperOrdersView(connection: connection, selectedOrderID: orderID)
                    case .unavailable:
                        EmptyView()
                    }
                }
                #else
                Label("iPhone 仅查看 · 请在有管理权限的 Mac 上创建和确认", systemImage: "eye")
                    .font(.caption).foregroundStyle(.secondary)
                    .accessibilityIdentifier("paper-proposal.read-only")
                #endif
            }
            .padding(12)
            .background(.background.opacity(0.7), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(.secondary.opacity(0.25)))
            .accessibilityIdentifier("paper-proposal.card")
        }
    }

    @ViewBuilder
    private func proposalSummary(_ proposal: PaperOrderProposal) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 5) {
            GridRow { Text("股票"); Text(proposal.symbol ?? "未提供") }
            GridRow { Text("方向"); Text(proposal.side?.rawValue ?? "未提供") }
            GridRow { Text("数量"); Text(proposal.quantity.map { "\($0) 股" } ?? "未提供") }
            GridRow { Text("限价"); Text(proposal.limitPrice.map { "\($0) USD" } ?? "未提供") }
            if let orderID = proposal.orderID {
                GridRow { Text("草稿 ID"); Text(orderID.uuidString.lowercased()).textSelection(.enabled) }
            }
            GridRow { Text("固定规则"); Text("Paper · STK / SMART / USD · LMT / DAY") }
        }
        .font(.caption)
    }
}
