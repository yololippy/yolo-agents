# Apps

`apps/` 只包含面向用户的应用层。每个 Agent 目录各自拥有 `ios/`、`macos/` 与 `xcode/` 工程配置，并依赖 `packages/sdk-swift`、`packages/ui-swift`；它们不直接拥有 Agent 业务逻辑。

- `trading-agent/`：当前交易 Agent 的 macOS、iOS 入口和 Xcode 项目。
- `b-agent/`：第二个 Agent 的 App 骨架。
