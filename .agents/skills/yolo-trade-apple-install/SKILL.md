---
name: yolo-trade-apple-install
description: "将当前 YoloTrade 客户端签名构建并安装或升级到本机 Mac、已连接的物理 iPhone，随后完成安装后的最小可用性确认。用于普通 Apple 客户端安装；不用于完整测试、单项测试、设备配对或产品代码修复。"
---

# YoloTrade Apple 安装

本 Skill 只负责 Apple 客户端的签名构建、安装或升级、回滚准备，以及安装后的最小可用性确认。不得在本流程中运行单元测试、集成测试、升级测试或 `yolo-agents-full-test`；用户明确要求这些测试时，退出本流程并使用对应入口。

## 路由

1. 从用户指令确定目标是 Mac、物理 iPhone 或两者；不能唯一确定时才询问。
2. 只安装 Mac 时读取 [macOS 安装](references/macos.md)；只安装 iPhone 时读取 [iOS 安装](references/ios.md)；两者都安装时读取两份参考。
3. 两端都安装时，使用互相独立且可复用的 DerivedData 并行完成签名构建；安装、启动和验收按平台分别记录结果。不要共享 DerivedData，也不要执行 `clean`。

## 共同门禁

- 记录当前 Git revision、工作区是否有差异，以及候选 App 的 Bundle ID、版本、构建号和签名身份；允许安装用户明确要求的未提交候选版本，但最终必须说明。
- 安装前确认 Host 身份为 `yolo-trade`，并保留 Paper、`read_only=false`、`execution_enabled=true`。Broker 必须为 `connected_paper`、`ready=true`、`data_complete=true`，且不存在 `submitting`、`unknown`、`cancel_pending` 未决订单。
- 本流程不得创建、确认、提交、撤销或重发订单，不得启用 Live，不得重启 IB Gateway。
- Apple 客户端安装默认不重启 Host。只有本次候选同时包含 Runtime 或 Host 运行配置变化时，才重启 YoloTrade Host；等待其恢复完整就绪后继续。
- 覆盖安装前核对现有 App 与候选 App 身份。Bundle ID、Team ID 或 designated requirement 不兼容时停止，不能通过卸载旧 App、清空 Keychain 或改签名绕过。
- Mac 保留可恢复的旧 App 备份；iPhone 只做同 Bundle ID 覆盖安装，不卸载旧 App。运行时设备标识、签名详情和本机路径不得写入版本控制文件。

## 安装后最小确认

安装完成不等于功能可用。完成目标平台参考中的身份、启动和界面检查后，再统一确认：

- Host 身份与客户端均为 YoloTrade，客户端在线并完成同步；
- Broker 仍满足安装前门禁，未出现未决订单；
- Provider 已授权、模型目录可用，并完成一次不启用业务工具、不保存会话、不会产生交易副作用的真实模型往返。

Mac 和 iPhone 同时安装时，真实模型往返只执行一次，不在两端重复。若 iPhone 安装后仍保持在线，不进入配对流程；只有确认连接凭据失效或用户明确要求重新配对时，才调用 `../yolo-agent-device-pairing/SKILL.md`。

## 耗时与汇报

分别记录并汇报签名构建、App 替换或传输、首次启动、功能恢复四段耗时。使用 `-showBuildTimingSummary` 定位编译瓶颈，保留平台独立 DerivedData 以复用增量结果；不要为提速跳过签名、身份校验、回滚或安装后确认。

在缓存有效且设备在线的正常情况下，以 Mac+iPhone 约 60–90 秒、单端约 35–60 秒作为观察目标，不作为成功门禁。App 替换通常少于 10 秒；超出目标时必须指出实际慢在构建、签名/provisioning、设备传输，还是 Host/IBKR 功能恢复。
