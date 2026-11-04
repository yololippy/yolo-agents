# YoloBeni iOS app

入口 `Sources/YoloBeniIOS/YoloBeniIOSApp.swift` 使用私有 `YoloBeniRootView`，工程为 `../xcode/YoloBeni.xcodeproj`，scheme 为 `YoloBeniIOS`，Bundle ID 为 `com.yoloagents.yolobeni.ios`。

提供聊天、模型选择、事件同步与脱敏账号状态，无账号管理和订单操作入口。先启动 YoloBeni runtime，再填写手机可访问的 Mac 地址（默认端口 8766），输入 YoloBeni 的 iOS 配对码。真机运行前自行配置签名 Team；本轮只构建 Simulator，不申请签名或安装。
