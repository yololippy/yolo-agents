# Shared packages

`packages/` 只放可被多个 Agent 或 App 复用、且职责单一的实现；不包含交易策略、券商账户或具体 Agent 配置。

- `protocol/`：跨端 API 契约、消息模型和兼容性规则。
- `core-python/`：设备认证、会话存储、聊天 API、PydanticAI 运行时和本机 Host。
- `sdk-swift/`：Swift 通信 SDK、SDK 测试和本地联调 probe。
- `ui-swift/`：依赖 `sdk-swift` 的可复用 SwiftUI 聊天界面。

新的共享能力按“职责-语言”命名，例如 `memory-python/`、`ui-swift/`。若只属于一个 Agent，应放在 `agents/<name>/`；只有多个 Agent 确实复用的第三方集成才创建 `integrations/<provider>-<language>/`。
