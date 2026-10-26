# Apple 客户端

Swift 6、Foundation、Security 和 SwiftUI；没有外部 Swift 包依赖。最低目标为 iOS 17 / macOS 14。

## 已实现

- `YoloAgentsSDK`：协议 v1 的 snake_case / UUID / 带时区 ISO8601 模型，设备配对、健康、会话列表/搜索/创建/改名/归档、消息、全局事件分页及自身设备令牌撤销。
- `YoloAgentsUI`：iPhone / iPad / Mac 自适应聊天界面、连接设置、会话切换、标题搜索、归档、流式文本、失败状态与手动重试。
- 全局游标增量轮询：正常每 2 秒一次，失败后最多退避到 30 秒；立即排空 `has_more`，处理并保存一页后才推进游标，按 seq 和消息 ID 去重。后台暂停、回前台恢复，撤销/失效令牌停止同步并提示重新配对。
- 发送前保存原始请求 UUID；网络结果不明时保留该 ID、原文和会话，手动重试取得原结果，不自动重放。202 只显示已送达，最终完成/失败由服务端消息和事件决定。
- 设备 token、服务地址、配对信息和离线工作集均保存在设备专属 Keychain，不使用 UserDefaults 保存聊天或凭据，不写日志。缓存最多 50 个最近会话、每会话 300 条消息，并有约 1.5 MB 的消息文本预算；未确认发送请求额外保留。完整历史仍以 Mac 服务数据库为准。
- iPhone 和 Mac 客户端当前均只有聊天与查看入口。没有订单确认、提交或可执行计划授权 UI；协议不包含模型框架内部对象。

## 构建与测试

在仓库的 `apps/trading-agent/xcode/` 目录中执行：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift build --package-path ../../../packages/sdk-swift
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --package-path ../../../packages/sdk-swift
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift build --package-path ../../../packages/ui-swift
```

2026-10-25 已用完整 **Xcode 27.1（27A9275）** 正常执行 `swift test`：25 项单元测试通过，1 项真实服务测试按默认条件跳过；同时构建了 macOS SwiftUI、SDK 和 probe，不需要指定 Testing 插件。

iOS 模拟器 SDK 的 Debug 通用目标已构建成功，使用 iPhoneSimulator 27.1 SDK，同时包含 arm64 / x86_64，最低目标仍是 iOS 17：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project YoloAgents.xcodeproj -scheme YoloAgentsIOS \
  -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /tmp/trading-agent-ios-derived-data \
  build
```

产物为 `/tmp/trading-agent-ios-derived-data/Build/Products/Debug-iphonesimulator/YoloAgentsIOS.app`；生成的 Info.plist 经 `plutil -lint` 检查通过。最初关闭签名的版本仅验证了编译，main 实际启动后遇到 Keychain -34018。工程现已对 Simulator 的 App / SDK 测试 target 配置本地 ad-hoc 签名和专用模拟器 entitlement；不要再传 `CODE_SIGNING_ALLOWED=NO` 运行该 App。`LOCALDEV00` 是仅用于 Simulator 的本地身份前缀，不是开发者账号；真机仍需用户自己的签名配置。Xcode 将模拟器权限嵌入 Mach-O 的 `__TEXT/__entitlements`，因此 `codesign -d --entitlements` 显示空字典不代表模拟器权限缺失。最新 `build-for-testing` 已成功，并检查嵌入的 application-identifier / keychain-access-groups 和完整签名。

早期只有 CLT 时，SwiftPM 的 TestingMacros 插件发现不稳定，曾通过显式加载其自带官方插件完成相同单元测试；该环境的旧构建系统还缺少 Testing framework 搜索路径。此记录保留为历史，当前使用上面的完整 Xcode 正常命令。代码中的 `ViewState` 是经典 SwiftUI `State` 属性包装器的类型别名，用于避开早期 CLT 未附带的同名宏插件；没有修改系统 SDK 或使用私有 API。

正式 App 工程为 `YoloAgents.xcodeproj`，共享 `YoloAgentsIOS` / `YoloAgentsMac` schemes，通过本地 Swift Package 引用通信和界面库。main 已统一准备 Simulator runtime 并完成下述 SDK / Keychain / 网络测试；正常启动后的 UI 交互仍需单独记录。真机运行前选择自己的签名 Team、唯一 Bundle Identifier 和已信任的设备。

`YoloAgentsMac` scheme 已用以下本地 ad-hoc 命令构建成功；产物在同一 DerivedData 的 `Build/Products/Debug/YoloAgentsMac.app`，本次未启动 Mac UI 或验收其真实 Keychain：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project YoloAgents.xcodeproj -scheme YoloAgentsMac -configuration Debug \
  -destination 'generic/platform=macOS' -derivedDataPath /tmp/trading-agent-ios-derived-data \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build
```

macOS 命令行原型与签名 App 的 Keychain 身份可能不同；调试重签名可能需要系统重新授权访问。纯 SDK 单元测试使用内存 SecureStore；iOS 专用测试仅操作唯一临时 service/account 并通过 defer 清理，不读取或删除应用已有 Keychain 条目。

## 连接服务

1. 按根目录 `packages/protocol/COMMUNICATION_V1.md` 启动服务，取得与当前设备平台对应的一次性配对码。
2. Mac 本机演示使用 `http://127.0.0.1:8765`，需要手动开启“允许本地开发连接（未加密）”。这不是默认授权。
3. iPhone 使用可达的 Mac **HTTPS** 服务地址；127.0.0.1 指向 iPhone 自身。服务证书必须受系统信任，客户端不绕过 TLS 验证。
4. 填写设备名称、配对码，配对后新建会话。`agent_mode=demo` 会明确显示“演示模式”，不表示已接真实模型或 IBKR。

非 HTTPS 地址只允许显式选择后的 localhost、规范十进制 RFC1918/回环 IPv4、IPv6 回环/ULA；公网 IP、其他 HTTP 主机名、URL 中的凭据/查询/片段均拒绝。URLSession 禁用 HTTP 持久缓存、cookies、凭据存储和全部重定向，防止凭据跟随到其他地址。连接配置从 Keychain 恢复时重新验证地址。

App Info.plist 只有本地网络声明和指定私有 IP/CIDR 的 ATS 例外，没有 `NSAllowsArbitraryLoads`。SDK 的显式选择和地址检查仍必须通过。iOS 17 / macOS 14 起，IP 的 ATS 例外与旧系统有差别，依据 [Apple NSAllowsLocalNetworking](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsallowslocalnetworking) 和 [NSExceptionDomains](https://developer.apple.com/documentation/bundleresources/information-property-list/nsapptransportsecurity/nsexceptiondomains) 配置；真机 HTTPS、权限弹窗及 ATS 行为还需实测。

“断开并清除本机记录”只移除本机 Keychain 条目；服务端撤销按协议的设备 DELETE 接口办理。缓存移除失败会显示错误，不宣称已经安全清空。

## 可选真实服务联调

`liveServiceRoundTrip` 默认跳过。仅在环境中同时提供 `YOLO_AGENTS_TEST_URL` 和 `YOLO_AGENTS_PAIRING_CODE` 时启用：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --package-path ../../../packages/sdk-swift --filter liveServiceRoundTrip
```

必须使用临时数据库和临时端口的 **demo** 服务。`YOLO_AGENTS_TEST_PLATFORM` 默认 `macos`，可显式设置为 `ios`；请求平台与配对响应都按传入值核对，配对码必须与该平台对应。测试通过 SDK 配对、创建中文会话、发送中文消息、同 ID 重发验证去重、以两条一页拉取事件直到最终回复、读取消息，并撤销它新建的设备后验证旧 token 收到 401/403。地址只允许客户端正常安全策略，最长等待回复 40 秒。测试不打印配对码或 token，不保存真实 Keychain；若测试中途异常，直接销毁临时服务数据即可清理测试设备。

CLT 测试插件缺失时，使用同一组环境变量执行无测试框架的联调程序：

```sh
swift run --build-system native --package-path ../../../packages/sdk-swift YoloAgentsProbe
```

入口是 `../../packages/sdk-swift/Sources/YoloAgentsProbe/Probe.swift`，仅依赖 Foundation、Darwin 和 `YoloAgentsSDK`，无需 Testing / XCTest 或 SwiftUI。main 的临时包 runner 可以只复制 SDK 和此入口，创建 library target `YoloAgentsSDK` 与 executable target `YoloAgentsProbe` 后运行。该程序还验证会话改名、搜索和归档，任何检查失败退出 1，成功输出不含凭据的 PASS；未提供两个环境变量时明确输出 SKIP。

2026-10-25，main 已使用临时 demo 服务执行以下真实 SDK 联调，`liveServiceRoundTrip` 1 项通过：

```sh
python3 bin/test-chat-integration.py --sdk-only --testing-plugin /Library/Developer/CommandLineTools/usr/lib/swift/host/plugins/testing/libTestingMacros.dylib
```

该命令在仓库根目录执行。已验证配对、中文消息、相同 client_message_id 重发去重、分页事件直到最终回复、历史读取及自身设备撤销后旧 token 收到 401。此结果来自真实本地 HTTP 服务与 Swift SDK 通信，不代表远程 HTTPS 或 iOS 真机验收；独立 probe 的额外检查不计入本次已通过范围。

## iOS SDK、Keychain 与网络测试 target

`YoloAgentsIOS` scheme 已包含 `YoloAgentsIOSSDKTests`，复用 `SDKTests.swift` / `LiveServiceTests.swift`，并加入 `KeychainTests.swift`。真实 Keychain 测试仅在 iOS 编译，唯一 service/account 都由 UUID 创建，验证不存在、写入、读取、更新、删除和重复删除；defer 只清理该临时条目。托管 App 在测试专用环境 `YOLO_AGENTS_RUNNING_SDK_TESTS=1` 下显示空视图，不恢复真实配对或聊天缓存。

已成功完成以下构建（不下载 runtime、不创建或启动 simulator）：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project YoloAgents.xcodeproj -scheme YoloAgentsIOS -configuration Debug \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /tmp/trading-agent-ios-derived-data build-for-testing
```

输出为 `TEST BUILD SUCCEEDED`。当前测试运行描述文件为：

`/tmp/trading-agent-ios-derived-data/Build/Products/YoloAgentsIOS_iphonesimulator27.1-arm64-x86_64.xctestrun`

这是 format 1 的标准 xctestrun，顶层 target 名称 `YoloAgentsIOSSDKTests`；App、测试 bundle 都已本地签名。main 使用 iPhone 17 Pro / iOS 27.0 完成了实际运行，最低部署版本仍为 iOS 17。

main 的统一 runner 可在仓库根目录运行，设备 ID 从外部提供：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer python3 bin/test-chat-integration.py \
  --ios-simulator "$YOLO_AGENTS_SIMULATOR_ID" \
  --xctestrun /tmp/trading-agent-ios-derived-data/Build/Products/YoloAgentsIOS_iphonesimulator27.1-arm64-x86_64.xctestrun
```

使用已有的外部临时服务时，将 `YOLO_AGENTS_TEST_URL`、`YOLO_AGENTS_PAIRING_CODE`、`YOLO_AGENTS_TEST_PLATFORM=ios` 注入**临时副本** xctestrun 的 `YoloAgentsIOSSDKTests.EnvironmentVariables`，保留 `YOLO_AGENTS_RUNNING_SDK_TESTS=1`；不要把配对码写入共享 scheme 或仓库。单纯设置 xcodebuild 自身进程的环境不保证传入 iOS 测试进程。副本保存在权限为 0600 的临时文件，移动副本前应将原始 `__TESTROOT__` 替换为原始 xctestrun 所在目录的绝对路径，保留 XCTest 自己解析的 `__TESTHOST__` 等占位符，再执行：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild test-without-building \
  -xctestrun "$YOLO_AGENTS_CONFIGURED_XCTESTRUN" \
  -destination "platform=iOS Simulator,id=$YOLO_AGENTS_SIMULATOR_ID" \
  -parallel-testing-enabled NO
```

统一 runner 会管理临时服务、一次性 ios 配对码及该配置副本。URL / 配对码未同时注入时，网络测试仍明确跳过；Keychain 和纯 SDK 测试独立运行。

**2026-10-25 实际验收：main 使用 Xcode 27.1（27A9275）构建的测试包，在 iPhone 17 Pro / iOS 27.0 Simulator 上执行统一 runner，27 项全部通过，无跳过。** 其中包含 25 项纯 SDK 测试、`isolatedKeychainWriteReadUpdateDelete` 和 `liveServiceRoundTrip`：

- 真实模拟器 Keychain：唯一临时 service/account 的写入、读取、更新、删除和重复删除成功，defer 清理完成；没有操作应用已有条目。
- 临时 demo 服务：ios 设备配对、中文消息发送、相同 client_message_id 重试去重、分页事件补齐至最终回复和历史读取成功。runner 另外检查了后端真实存在 completed 回复，排除了测试跳过导致的误报。
- 自身设备撤销后旧 token 被拒绝；本轮临时设备已撤销，临时服务和数据库均已清理。
- main 随后正常启动更新后的 App 并检查首屏截图，确认此前的 Keychain -34018 弹窗消失；聊天等完整 UI 操作仍需独立验收。

本轮验证使用本地网络回环与 demo 模式，没有调用云端模型，也不代表真机或外网连接验收。首屏缓存说明现采用普通用户文案，不展示会话/消息上限或 Keychain 实现细节；默认连接安全策略、开发连接选择和缓存逻辑保持原有实现。此次证据与文案更新仅做必要编译检查，没有重复运行已通过的 27 项测试。

## 验收边界

2026-10-25，Apple Silicon 上的完整 Xcode 27.1（27A9275）已验证共享 SDK 的正常单元测试、macOS SwiftUI / probe 编译、iOS Simulator SDK 的通用 Debug 构建，以及 iPhone 17 Pro / iOS 27.0 Simulator 上 27 项全部通过的 SDK / 真实 Keychain / 临时 demo 网络测试。工程和产物配置经 plist/XML 语法检查。

后续仍需单独验收：正常 Simulator App 的 UI 交互、iPhone 真机和开发签名、云端模型真实调用、外网与远程 HTTPS / ATS / 本地网络权限、应用缓存的完整保存/恢复/清理、推送以及弱网长时运行。模拟器和 Mac 的本地签名构建不代表真机或分发归档通过；Mac UI 和真实 Keychain 仍未实测。

通知推送、结构化交易结果卡片、完整聊天正文搜索和生产分发不在当前协议/交付中；消息里的结果文本按服务端内容展示。实时更新当前采用协议允许的增量轮询，没有另接 SSE，也没有让 iOS 后台持续轮询。
