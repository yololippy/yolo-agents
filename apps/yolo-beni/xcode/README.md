# YoloBeni Xcode project

`YoloBeni.xcodeproj` 提供 `YoloBeniMac` / `YoloBeniIOS` schemes，通过 `../client-swift` 复用 SDK/UI。产品名、Bundle ID 和存储独立，无 YoloTrade 或 Paper 依赖。

产品显示名、两端 Bundle ID 和版本统一定义在 `Configuration/Brand.xcconfig`；修改品牌配置时无需在 Info.plist 或各 target 重复维护。

在本目录构建，不安装软件或申请签名：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project YoloBeni.xcodeproj -scheme YoloBeniMac -configuration Debug \
  -destination 'generic/platform=macOS' -derivedDataPath /tmp/yolo-provider-yolo-beni \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project YoloBeni.xcodeproj -scheme YoloBeniIOS -configuration Debug \
  -destination 'generic/platform=iOS Simulator' -derivedDataPath /tmp/yolo-provider-yolo-beni \
  CODE_SIGNING_ALLOWED=NO build
```

Mac 产物：`/tmp/yolo-provider-yolo-beni/Build/Products/Debug/YoloBeniMac.app`。准备好 YoloBeni runtime 和配对码后，可手动执行 `open -n /tmp/yolo-provider-yolo-beni/Build/Products/Debug/YoloBeniMac.app`。Simulator 产物：`/tmp/yolo-provider-yolo-beni/Build/Products/Debug-iphonesimulator/YoloBeniIOS.app`。

2026-10-28：两端 arm64 / x86_64 通用目标构建通过。随后已启动 Mac App，完成独立配置的真实授权、配对与界面发送验收；验收使用 YoloBeni 自己的 Keychain，未访问 YoloTrade 生产配对。未安装 Simulator App 或进行 iPhone 真机验收。详情见 [TODO](../../../docs/TODO.md)。

上面的 iOS 命令仅验证编译。集成阶段若需运行 Simulator，请去掉 `CODE_SIGNING_ALLOWED=NO`，使用工程预置的本地 ad-hoc 签名和 Simulator 专属 entitlement；真机签名另行配置。
