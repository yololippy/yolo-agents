# YoloTrade Apple 客户端

YoloTrade 的 SwiftUI macOS/iOS 客户端，最低 macOS 14、iOS 26。私有 `client-swift` 模块注入 YoloTrade 身份、Paper 订单模型和界面；通用聊天、账号、连接与同步来自共享 SDK/UI。

## 身份与结构

| 配置 | 值 |
|---|---|
| Agent ID / 名称 | `yolo-trade` / `YoloTrade` |
| 默认 Host | `http://127.0.0.1:8765` |
| Keychain service | `com.yoloagents.yolo-trade.apple.v1` |
| Bundle ID | `com.yoloagents.yolotrade` |
| Xcode schemes | `YoloTradeMac` / `YoloTradeIOS` |

```text
client-swift/                  私有根视图、Paper 客户端和测试
macos/Sources/YoloTradeMac/   macOS App 入口
ios/Sources/YoloTradeIOS/     iOS App 入口
xcode/YoloTrade.xcodeproj     两端工程
xcode/Configuration/          品牌、Bundle ID、版本与 entitlement
```

产品显示名、Bundle ID 和版本统一定义在 `xcode/Configuration/Brand.xcconfig`。共享层不包含 IBKR、Paper 模型或产品判断。

## 连接与同步

先启动 [YoloTrade Runtime](../../agents/yolo-trade/README.md)，再使用对应平台的一次性配对码。

- Mac 本地开发可以显式允许 `http://127.0.0.1:8765`。
- iPhone 必须使用可访问的 Mac 地址；127.0.0.1 指向手机自身。
- 正式远程连接必须使用系统信任的 HTTPS，客户端不绕过 TLS。
- REST/SSE 使用设备 Bearer 凭据，禁用重定向、cookies、持久 HTTP 缓存和系统代理继承。
- 客户端先持久化状态再推进事件游标；断线后用 REST 补齐，再恢复 SSE。
- 网络结果不确定的写请求不会自动重放。

设备 token、服务地址、配对和离线工作集保存在产品专属 Keychain。断开本机记录不等于撤销服务端设备；撤销必须调用认证接口。

## Paper 订单

YoloTrade 私有根视图通过通用 `ChatFeature` 注入“交易”页面：

- macOS 与 iOS 都可查看脱敏持仓、IBKR 股票搜索、参考行情、订单详情和事件。
- 从持仓或搜索结果进入快速票据时固定 `con_id` 和主交易所；数量与限价由用户输入，参考行情不会自动填入限价。
- Mac 管理设备可在页面中单独授权或撤销已配对 iPhone 的 Paper 交易权限。未授权 iPhone 保持只读；授权 iPhone 可创建、What-if、确认、提交和单笔撤单。
- 快速单绕过 AI，但继续复用同一订单账本、硬风控、明确确认、幂等与 unknown 不重发门禁；实际券商调用始终由 Mac Host 发起。
- 账户只使用服务端健康响应中的不透明 `config_id`，不提供账户号输入。

`PaperOrdersModel` 将创建、预览、确认、提交和撤单拆成独立动作。每次写操作使用独立 UUID，并绑定最新订单版本、content hash、预览或确认 ID。执行前重新读取健康和订单；超时或无效响应显示待核对，不自动重试。

聊天订单卡只接受明确标记的 `paper-order`/`json` 代码块和严格类型。服务端已创建的 `paper_order_draft` 必须携带真实 `order_id`；普通文字或未标记 JSON 不会被推断为订单。模型不能执行 What-if、确认或提交。

完整订单契约见 [Paper 订单 v1](../../agents/yolo-trade/PAPER_ORDERS.md)。

## 构建与测试

从仓库根目录运行：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  swift test --package-path packages/sdk-swift

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  swift test --package-path packages/ui-swift

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  swift test --package-path apps/yolo-trade/client-swift

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project apps/yolo-trade/xcode/YoloTrade.xcodeproj \
  -scheme YoloTradeMac -configuration Debug \
  -destination 'generic/platform=macOS' \
  -derivedDataPath /tmp/yolo-trade-derived-data \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build

DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project apps/yolo-trade/xcode/YoloTrade.xcodeproj \
  -scheme YoloTradeIOS -configuration Debug \
  -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /tmp/yolo-trade-derived-data build
```

Simulator App 和 SDK 测试 target 使用本地 ad-hoc 签名与专属 entitlement，以便访问测试 Keychain；不要通过 `CODE_SIGNING_ALLOWED=NO` 启动需要 Keychain 的 App。真机和分发构建必须使用用户自己的签名 Team。

可选真实服务联调只允许临时 demo 服务和一次性配对码：

```sh
YOLO_AGENTS_TEST_URL='<临时服务地址>' \
YOLO_AGENTS_PAIRING_CODE='<一次性配对码>' \
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
swift test --package-path packages/sdk-swift --filter liveServiceRoundTrip
```

当前验证基线、真实 Paper、远程网络和真机待办见 [ROADMAP](../../docs/ROADMAP.md)。
