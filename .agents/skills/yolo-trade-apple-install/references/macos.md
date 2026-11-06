# macOS 安装

仅在目标包含当前 Mac 时读取。

## 签名构建

1. 使用 `.agents/runtime/yolo-trade-apple-install/derived-macos/` 作为可复用的独立 DerivedData。不得与 iOS 共用，不执行 `clean`，也不因普通编译失败直接删除缓存。
2. 从仓库根目录构建 `YoloTradeMac` 的 arm64 Debug 产物，保留工程中的 Apple Development 自动签名：

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
/usr/bin/xcodebuild \
  -project apps/yolo-trade/xcode/YoloTrade.xcodeproj \
  -scheme YoloTradeMac -configuration Debug \
  -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath .agents/runtime/yolo-trade-apple-install/derived-macos \
  -skipMacroValidation ARCHS=arm64 ONLY_ACTIVE_ARCH=YES \
  SWIFT_ENABLE_EXPLICIT_MODULES=NO \
  -showBuildTimingSummary build
```

3. 候选产物是 `derived-macos/Build/Products/Debug/YoloTradeMac.app`。使用 `codesign --verify --deep --strict`、`codesign -dvv` 和 `codesign -d -r-` 核对：
   - Bundle ID 为 `com.yoloagents.yolotrade`；
   - Team ID 与当前已安装 App 一致；
   - designated requirement 使用稳定 Apple 签名，不是依赖 CDHash 的 ad-hoc 身份。

首次构建慢通常来自 Swift Package 解析、全量编译和签名。增量安装必须复用上述 DerivedData；不要同时再构建 Simulator 或运行测试。只有 Xcode 明确报告 provisioning 无法更新，且当前 Xcode 账户已获得用户授权时，才针对同一命令增加 `-allowProvisioningUpdates` 后重试一次。

## 安全替换

1. 通过 Bundle ID 找到当前安装位置；结果必须唯一。首次安装时，将目标明确解析为当前用户的 `Applications/YoloTrade.app`，不得用未解析的 `~`、宽泛目录或搜索结果直接做替换目标。
2. 请求现有 YoloTrade App 正常退出并确认进程结束。不要终止 Host 或 IB Gateway。
3. 使用 `agents/yolo-trade/runtime-python/yolo_trade/apple_upgrade.py` 中的 `replace_macos_app` 执行替换：
   - 候选路径使用本次构建产物；
   - 目标使用上一步核验的唯一安装位置；
   - 备份目录使用当前用户产品私有的 `Library/Application Support/YoloAgents/yolo-trade/app-backups/`。
4. 该入口会再次校验 Bundle ID、签名和 designated requirement，通过 `ditto` 保留扩展属性，在同一父目录原子替换，并保留旧 App 备份。不要改用 `rm -rf`、直接覆盖复制或先删后装。

## 启动与确认

1. 通过 Bundle ID 启动新 App，核对运行中的可执行文件来自目标安装位置，版本和构建号与候选一致。
2. 在可见 UI 中确认“交易”入口存在，Host、AI 和 IBKR 均显示已连接；交易页能加载持仓或股票搜索能力即可，不创建订单。
3. 从服务端再次核对 Host/Broker/未决订单状态。若失败，安装结论标为未完成，并保留备份路径；只有明确确认新 App 导致不可用时，才用同一安全替换入口恢复备份。
