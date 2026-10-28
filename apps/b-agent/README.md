# B Agent apps

独立的 SwiftUI macOS / iOS 客户端，最低 macOS 14 / iOS 17。私有 `client-swift/` 根视图注入身份并复用共享 SDK/UI，功能列表为空，不依赖 Trading 或提供订单入口。

| 配置 | 值 |
|---|---|
| Agent ID / 名称 | `b-agent` / B Agent |
| 默认 Mac 服务 | `http://127.0.0.1:8766` |
| Keychain service | `com.yoloagents.b-agent.apple.v1` |
| 配对/缓存键前缀 | `agent.b-agent.` |
| Bundle IDs | `com.yoloagents.bagent.macos` / `com.yoloagents.bagent.ios` |
| Xcode schemes | `BAgentMac` / `BAgentIOS` |

先按 [B runtime](../../agents/b-agent/runtime-python/README.md) 启动自己的服务，使用其生成的 Mac / iOS 配对码。手机填写可访问的 Mac 地址，不能用手机自身 localhost。B 的健康响应必须明确为 `agent_id:b-agent`；不读取或迁移 Trading 的旧配对、缓存或授权。

Mac 的“ChatGPT 账号”经当前 B 服务发起浏览器授权，iOS 只看脱敏状态。OAuth 凭据留在后端。构建和产物见 [Xcode 工程](xcode/README.md)。2026-10-28 已完成真实授权及 Mac 连接、账号页、历史同步与 Enter 单次发送验收，使用独立验收服务 `http://127.0.0.1:18766`。默认开发端口仍为 8766，iPhone 真机未验收。证据见 [TODO](../../docs/TODO.md)。
