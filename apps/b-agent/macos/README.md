# B Agent macOS app

入口 `Sources/BAgentMac/BAgentMacApp.swift` 使用私有 `BAgentRootView`，工程为 `../xcode/BAgent.xcodeproj`，scheme 为 `BAgentMac`，Bundle ID 为 `com.yoloagents.bagent.macos`。

窗口名为 B Agent，默认服务 `http://127.0.0.1:8766`，配对和缓存使用 B 自己的 Keychain service。启动 B runtime 后输入其 Mac 配对码，再通过“ChatGPT 账号”进行官方浏览器登录、取消或退出 B 授权。无订单入口。
