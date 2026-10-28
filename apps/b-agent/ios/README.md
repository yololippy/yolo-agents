# B Agent iOS app

入口 `Sources/BAgentIOS/BAgentIOSApp.swift` 使用私有 `BAgentRootView`，工程为 `../xcode/BAgent.xcodeproj`，scheme 为 `BAgentIOS`，Bundle ID 为 `com.yoloagents.bagent.ios`。

提供聊天、模型选择、事件同步与脱敏账号状态，无账号管理和订单操作入口。先启动 B runtime，再填写手机可访问的 Mac 地址（默认端口 8766），输入 B 的 iOS 配对码。真机运行前自行配置签名 Team；本轮只构建 Simulator，不申请签名或安装。
