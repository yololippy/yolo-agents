# iOS 真机安装

仅在目标包含物理 iPhone 时读取。本流程不用于 Simulator。

## 发现设备与签名构建

1. 设置 `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`，使用 `xcrun devicectl list devices` 发现本轮实际连接且可用的物理 iPhone。不得保存或写死设备 ID；多台可用设备且用户没有指定时才询问。
2. iPhone 必须已解锁、信任当前 Mac 并启用 Developer Mode。只有系统要求解锁、信任或本人确认时才让用户完成该单一步骤。
3. 使用 `.agents/runtime/yolo-trade-apple-install/derived-ios/` 作为可复用的独立 DerivedData。以发现的设备作为 destination，构建 `YoloTradeIOS` Debug 真机产物：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
/usr/bin/xcodebuild \
  -project apps/yolo-trade/xcode/YoloTrade.xcodeproj \
  -scheme YoloTradeIOS -configuration Debug \
  -destination 'id=<本轮发现的设备 ID>' \
  -derivedDataPath .agents/runtime/yolo-trade-apple-install/derived-ios \
  -skipMacroValidation SWIFT_ENABLE_EXPLICIT_MODULES=NO \
  -showBuildTimingSummary build
```

4. 候选产物是 `derived-ios/Build/Products/Debug-iphoneos/YoloTradeIOS.app`。安装前核对代码签名有效、Bundle ID 为 `com.yoloagents.yolotrade`、Team ID 与设备上当前 App 一致。

设备构建慢通常来自首次真机架构编译、自动签名和 provisioning。复用上述 DerivedData，不并带 Simulator 构建或测试。只有 Xcode 明确报告 provisioning 无法更新，且当前 Xcode 账户已获得用户授权时，才增加 `-allowProvisioningUpdates` 重试一次；不得改 Team 或 Bundle ID 规避签名错误。

## 覆盖安装

1. 使用 `devicectl device info apps` 查询设备现状。若存在旧 Bundle ID `com.yoloagents.tradingagent`，这是身份迁移，停止普通覆盖安装；不得卸载后冒充升级。
2. 使用 `agents/yolo-trade/runtime-python/yolo_trade/apple_upgrade.py` 中的 `replace_ios_app` 执行同 Bundle ID 覆盖安装。已有当前 App 时保持 `require_existing=true`；仅在用户明确要求首次安装且已确认不存在旧身份 App 时才允许 `require_existing=false`。
3. 该入口通过 `devicectl device install app` 安装并重新读取设备 App 列表，核对版本和构建号。不要先卸载 App，不清空设备 Keychain，不因安装失败自动重新配对。
4. 安装完成后使用 `devicectl device process launch` 按 Bundle ID 启动 YoloTrade。

## 界面与连接确认

1. 优先使用 `devicectl device capture screenshot --device <本轮设备> --destination <运行时临时 PNG>` 获取当前可见界面，确认新底部“交易”入口或目标版本界面已经出现；截图只放在 `.agents/runtime/` 或系统临时目录，不提交仓库。
2. 只有必须点击、处理前台权限弹窗或完成用户已授权的交互时，才打开 iPhone Mirroring。不要为只读确认默认进入镜像自动化。
3. 同时从服务端确认该 iOS 客户端在线并完成同步，Host 身份、Broker 状态和未决订单门禁仍通过。
4. 若 App 回到配对页或服务端确认凭据失效，停止把安装表述为完成，再进入设备配对 Skill；普通离线、屏幕锁定或短暂同步不先重装、不先重新配对。
