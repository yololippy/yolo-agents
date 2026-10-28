# UI Swift

可复用的 SwiftUI 聊天界面。该包依赖相邻的 `sdk-swift`，不包含任何特定 Agent 的品牌、Bundle ID 或 App 工程配置。

2026-10-27：

- `ChatModel` 先补齐全局游标，再持续接收 SSE；正常服务端流轮换直接续接，网络错误按退避重试，设备认证失效停止连接。iOS 在非活动阶段取消，恢复活动后补齐；macOS 不因窗口短暂失去焦点中断回复。
- 模型选择按会话保存，只在 PATCH 成功后显示新模型。目录的服务错误和实际选择错误分别展示；认证或套餐能力仍由服务端判定。
- 聊天页左上角提供 `square.and.pencil` 新会话按钮，Mac 支持 ⌘N；会话列表保留同名入口。
- `ChatComposer` 在 iOS 使用原生 `TextEditor` 与自身 FocusState，Mac 使用 AppKit `NSTextView`，都放在单一底部安全区中；按会话保持稳定身份，避免消息列表刷新改变编辑控件。连接状态只影响发送，不禁用已有会话的草稿编辑；归档会话不可编辑。Mac Enter 发送、Shift+Enter 换行；没有额外的 Cmd+Return 发送快捷键。中文组字时先交由输入法处理。
- `ComposerDrafts` 按会话保留草稿；晚到的发送结果只清除未被继续编辑的原草稿。

测试：`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test`。状态测试覆盖 SSE 增量、断线补齐/去重、后台取消、非活动启动、模型错误与草稿行为；它们不替代 iPhone 系统键盘的真机验收。

## Mac 输入与 Codex 账号

Mac 编辑器将 `textContainerInset` 设为 `(12,10)`、`lineFragmentPadding` 设为 `0`；正文和 placeholder 使用相同 AppKit 字体、段落和文字容器布局。组字期间不以外部更新覆盖 marked text。点击发送按钮后通过原生 first responder 恢复焦点。

侧边栏和聊天工具栏提供“Codex 账号”。Mac 管理设备读取邮箱、套餐和登录状态，发起浏览器登录后轮询账号状态，退出必须先确认影响本机 Codex 受管登录。iOS 及非管理设备只使用模型目录中的脱敏认证状态，不调用私有账号接口。邮箱和套餐只在内存展示，不加入聊天缓存。

账号单测使用隔离 HTTP fixtures，不打开浏览器、不修改本机 Codex 登录。UI 包 17 项测试通过，包括 MacReturnAction、原生 Enter 单次提交、Shift+Enter 换行、排版参数、账号权限、登录轮询/取消/超时及退出失败保留状态。

## App 功能入口

`YoloAgentsRootView(features:)` 接收 App 注入的 `ChatFeature`，在侧边栏和工具栏展示入口，并将当前配对连接传给弹出页面。默认列表为空，其他 Agent 不会自动获得 Trading Agent 的订单入口。共享 UI 不持有 Paper 模型或交易动作；实现与测试见 [Trading Agent 客户端](../../apps/trading-agent/client-swift/README.md)。
