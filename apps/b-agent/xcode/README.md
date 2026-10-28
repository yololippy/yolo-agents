# B Agent Xcode project

`BAgent.xcodeproj` 提供 `BAgentMac` / `BAgentIOS` schemes，通过 `../client-swift` 复用 SDK/UI。产品名、Bundle ID 和存储独立，无 Trading 或 Paper 依赖。

在本目录构建，不安装软件或申请签名：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project BAgent.xcodeproj -scheme BAgentMac -configuration Debug \
  -destination 'generic/platform=macOS' -derivedDataPath /tmp/yolo-provider-b-agent \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project BAgent.xcodeproj -scheme BAgentIOS -configuration Debug \
  -destination 'generic/platform=iOS Simulator' -derivedDataPath /tmp/yolo-provider-b-agent \
  CODE_SIGNING_ALLOWED=NO build
```

Mac 产物：`/tmp/yolo-provider-b-agent/Build/Products/Debug/BAgentMac.app`。准备好 B runtime 和配对码后，可手动执行 `open -n /tmp/yolo-provider-b-agent/Build/Products/Debug/BAgentMac.app`。Simulator 产物：`/tmp/yolo-provider-b-agent/Build/Products/Debug-iphonesimulator/BAgentIOS.app`。

2026-10-28：两端 arm64 / x86_64 通用目标构建通过。随后已启动 Mac App，完成独立配置的真实授权、配对与界面发送验收；验收使用 B 自己的 Keychain，未访问 Trading 生产配对。未安装 Simulator App 或进行 iPhone 真机验收。详情见 [TODO](../../../docs/TODO.md)。

上面的 iOS 命令仅验证编译。集成阶段若需运行 Simulator，请去掉 `CODE_SIGNING_ALLOWED=NO`，使用工程预置的本地 ad-hoc 签名和 Simulator 专属 entitlement；真机签名另行配置。
