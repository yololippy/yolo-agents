# UI Swift

可复用的 SwiftUI 聊天界面。该包依赖相邻的 `sdk-swift`，不包含任何特定 Agent 的品牌、Bundle ID 或 App 工程配置。

2026-10-27：

- `ChatModel` 先补齐全局游标，再持续接收 SSE；正常服务端流轮换直接续接，网络错误按退避重试，设备认证失效停止连接。iOS 在非活动阶段取消，恢复活动后补齐；macOS 不因窗口短暂失去焦点中断回复。
- 模型选择按会话保存，只在 PATCH 成功后显示新模型。目录的服务错误和实际选择错误分别展示；认证或套餐能力仍由服务端判定。
- 聊天页左上角提供 `square.and.pencil` 新会话按钮，Mac 支持 ⌘N；会话列表保留同名入口。
- `ChatComposer` 在 iOS 使用原生 `TextEditor` 与自身 FocusState，Mac 使用 AppKit `NSTextView`，都放在单一底部安全区中；按会话保持稳定身份，避免消息列表刷新改变编辑控件。连接状态只影响发送，不禁用已有会话的草稿编辑；归档会话不可编辑。Mac Enter 发送、Shift+Enter 换行；没有额外的 Cmd+Return 发送快捷键。中文组字时先交由输入法处理。
- `ComposerDrafts` 按会话保留草稿；晚到的发送结果只清除未被继续编辑的原草稿。

测试：`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test`。状态测试覆盖 SSE 增量、断线补齐/去重、后台取消、非活动启动、模型错误与草稿行为；它们不替代 iPhone 系统键盘的真机验收。

## Mac 输入与 ChatGPT 账号

Mac 编辑器保留原生 NSTextView、中文组字与 Enter / Shift+Enter 行为。

侧边栏和工具栏提供“ChatGPT 账号”。`ChatGPTAccountModel` 展示所属 App，默认使用通用账号接口；只有允许旧服务的 Trading 在 GET 404 后发现旧 Codex 读接口。POST 不回退。Mac 发起官方浏览器授权、轮询状态；取消和超时真正调用后端取消接口，取消失败保持未确认提示，旧 Codex 模式不支持取消时显示实际错误。

Provider 退出仅针对当前 Agent；`method:codex` 或旧接口保留 Codex 受管登录对其他使用者的影响说明。`expired` 显示“登录状态需要刷新”，`permission_required` 显示待授予套餐权限，两者仍能退出清理。远程撤销未确认会明确显示。iOS 和非管理设备只读健康身份与模型目录中的脱敏认证状态，邮箱和套餐不进入其状态或聊天缓存。

2026-10-28：共享 UI 23 项测试通过，覆盖输入、聊天、浏览器登录轮询、实际取消/超时、退出、旧路由、状态清理与认证前阻断错误 Agent。测试使用内存 fixtures，不打开浏览器或访问生产 Keychain。

## App 功能入口

`YoloAgentsRootView(identity:features:)` 接收私有根视图注入的 `AgentAppIdentity` 与可选 `ChatFeature`。身份决定品牌、默认地址和存储；功能入口默认空，B 不链接或展示订单。共享 UI 不持有 Paper 模型或交易动作，见 [Trading 私有客户端](../../apps/trading-agent/client-swift/README.md)。

首次连接页的标题取当前 App 身份，B 显示 B Agent。配对回归测试使用不含交易能力字段的 B 健康响应，验证完成配对并保存正确 Agent ID。
