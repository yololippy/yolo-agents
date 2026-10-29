# Trading Agent 客户端模块

此模块属于 Trading Agent，由 macOS 和 iOS 入口共同使用，不是跨 Agent 共享包。`TradingAgentRootView` 向共享聊天壳注入订单入口；`PaperAPIClient`、`PaperHealth`、订单模型、Writer 与界面均在这里。公共聊天与传输继续依赖 `packages/ui-swift` 和 `packages/sdk-swift`。

根视图注入 `trading-agent` 身份、Trading Agent 名称、端口 8765 及原有 `com.tradingagent.apple.v1` Keychain service，保留配对和历史。仅此 App 显式允许缺少身份的旧服务，并在连接设置中说明兼容边界；明确返回其他 Agent 时拒绝。订单健康检查和 Writer 也拒绝其他 Agent。

订单协议见 [Paper 订单 v1](../../../agents/trading-agent/PAPER_ORDERS.md)。

从仓库根目录执行测试：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --package-path apps/trading-agent/client-swift
```

## Paper 订单

`paperBrokerHealth()`、`paperOrders()`、`paperOrder(id)` 为两端提供带设备认证的脱敏状态、订单和事件。`PaperHealth.broker` 可空，兼容旧健康响应；执行门槛字段缺失时视为未就绪。账户选择只能从已配置的 Paper `broker.configuration.config_id` 构造，不接收账户号。

macOS 编译才提供 `PaperOrderWriter` 和五种严格类型请求，分别创建草稿、预览、确认、提交和单笔撤单。Writer 初始化要求有效 Mac 管理设备；服务端仍逐次重新认证并校验执行门槛。草稿固定美股 STK / SMART / USD、LMT / DAY，数量为整数，限价为最多四位小数的正十进制文本。后续请求只携带对应操作规定的版本、hash、预览或确认 ID 及固定 `user_action`，没有通用交易字段入口。`OrderExpiry` 原样编码服务端时间文本，避免期限绑定因格式转换失败。

每个请求显式接收 `clientRequestID`，同一请求值可保留相同正文和 ID；Writer 每次调用只发送一次，不执行网络重试。未知状态保留原始值并禁止写操作。iOS 编译完全移除 Writer 与写请求类型。

订单测试使用内存 HTTP fixtures，覆盖精确字段编码、幂等 ID、原始期限、失效绑定、输入与角色校验、缺失执行门槛及超时不重试；不连接 IBKR。

## Paper 订单（2026-10-28）

聊天侧边栏和工具栏的“Paper 订单”打开交易应用的列表、详情和事件历史。iOS 只读，编译时没有订单写方法和操作控件。Mac 管理设备可创建美股限价草稿，账户目标只采用已认证健康响应中的不透明配置 ID，无账户输入框。

`PaperOrdersModel` 将创建、What-if 预览、确认预览、提交及单笔撤单分开。预览展示确认的合约、保证金、佣金及期限；确认、提交和撤单各自需要核对弹窗。弹窗绑定不可变订单快照和一次性本地 review ID，执行前重新读取健康与当前订单；每次操作使用独立 UUID。刷新、选择、视图重建及恢复都只读取数据，重新打开的 confirmed 订单不会自动获得提交能力。

请求发出后的超时或无效响应显示待核对状态，不自动重发。`unknown`、`submitting`、`cancel_pending` 明确展示为尚未确定；撤单处理中不显示为已撤销。成交数量不回退，完整成交不会被迟到撤单覆盖。状态更新需要用户刷新；没有后台发单或自动续提交流程。

聊天消息只会把明确标记的 fenced JSON 识别为订单卡片：代码块语言为 `paper-order` 或 `json`，且 `type` 必须是 `paper_order_proposal` 或 `paper_order_draft`。字段沿用草稿协议中的 `symbol`、`side`、整数 `quantity` 和字符串 `limit_price`；可选的 `environment`、`order_type`、`tif` 只能分别为 `paper`、`LMT`、`DAY`。普通文字、未标记 JSON 和其他类型不会被推断为订单。

`paper_order_draft` 表示模型工具已经在服务端创建了非执行草稿，必须携带工具成功结果中的 UUID `order_id`。Mac 卡片只按该 ID 读取并选中已有订单，读取失败时不会创建替代草稿；模型回显的其他字段只用于消息摘要，订单详情以服务端为准。只有不含 `order_id` 的 `paper_order_proposal` 才能预填新草稿表单，缺失或无效字段必须人工补全。iOS 两种卡片都仅展示。模型不能执行 What-if、确认或提交。

新增订单状态测试使用纯内存模拟服务，覆盖独立动作、重复点击、取消核对、超时、恢复、iOS/非管理设备只读、版本和会话变化、预览过期/不完整、无效响应、结构化聊天提案、已有模型草稿路由及完整成交优先。私有客户端合计 25 项测试通过；这些测试不代替真实 Paper 环境和真机交互验收。
