# YoloBeni Apple 客户端

YoloBeni 的 SwiftUI macOS/iOS 客户端，最低 macOS 14、iOS 26。私有 `client-swift` 模块注入 YoloBeni 身份并复用共享 SDK/UI，不链接 YoloTrade 或订单能力。

## 身份与入口

| 配置 | 值 |
|---|---|
| Agent ID / 名称 | `yolo-beni` / `YoloBeni` |
| 默认 Host | `http://127.0.0.1:8766` |
| Keychain service | `com.yoloagents.yolo-beni.apple.v1` |
| 缓存键前缀 | `agent.yolo-beni.` |
| Bundle IDs | `com.yoloagents.yolobeni.macos` / `com.yoloagents.yolobeni.ios` |
| Xcode schemes | `YoloBeniMac` / `YoloBeniIOS` |

主要文件：

```text
client-swift/                  私有根视图与产品配置
macos/Sources/YoloBeniMac/    macOS App 入口
ios/Sources/YoloBeniIOS/      iOS App 入口
xcode/YoloBeni.xcodeproj      两端工程
xcode/Configuration/          品牌、Bundle ID、版本与 entitlement
```

产品显示名、Bundle ID 和版本统一定义在 `xcode/Configuration/Brand.xcconfig`。共享 UI 提供聊天、账号、连接和 iOS 26 玻璃功能坞；YoloBeni 当前只显示通用账号与连接入口。

## 连接

先启动 [YoloBeni Runtime](../../agents/yolo-beni/README.md)，再使用它生成的 Mac/iOS 配对码。

- Mac 默认连接 `127.0.0.1:8766`。
- iPhone 必须填写可访问的 Mac 地址，不能使用手机自己的 localhost。
- 服务健康响应必须明确返回 `agent_id: yolo-beni`。
- 客户端不会读取或迁移 YoloTrade 的配对、缓存、Keychain 或授权。
- Mac 可以管理当前 YoloBeni 的 ChatGPT 账号；iOS 只显示脱敏状态。

## 构建与测试

从仓库根目录运行：

```sh
swift test --package-path apps/yolo-beni/client-swift

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project apps/yolo-beni/xcode/YoloBeni.xcodeproj \
  -scheme YoloBeniMac -configuration Debug \
  -destination 'generic/platform=macOS' \
  -derivedDataPath /tmp/yolo-beni-derived-data \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project apps/yolo-beni/xcode/YoloBeni.xcodeproj \
  -scheme YoloBeniIOS -configuration Debug \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /tmp/yolo-beni-derived-data \
  CODE_SIGNING_ALLOWED=NO build
```

上面的 iOS 命令只验证编译。运行 Simulator 时使用工程提供的 Simulator entitlement；真机需要单独配置签名和网络权限。

当前验证基线和真机待办见 [ROADMAP](../../docs/ROADMAP.md)。
