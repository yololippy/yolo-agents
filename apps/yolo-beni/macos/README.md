# YoloBeni macOS app

入口 `Sources/YoloBeniMac/YoloBeniMacApp.swift` 使用私有 `YoloBeniRootView`，工程为 `../xcode/YoloBeni.xcodeproj`，scheme 为 `YoloBeniMac`，Bundle ID 为 `com.yoloagents.yolobeni.macos`。

窗口名为 YoloBeni，默认服务 `http://127.0.0.1:8766`，配对和缓存使用 YoloBeni 自己的 Keychain service。启动 YoloBeni runtime 后输入其 Mac 配对码，再通过“ChatGPT 账号”进行官方浏览器登录、取消或退出 YoloBeni 授权。无订单入口。
