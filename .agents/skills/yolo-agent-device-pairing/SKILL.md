---
name: yolo-agent-device-pairing
description: "通过 Mac 的 iPhone Mirroring 为 yolo-agents 的 iOS 客户端配对或重新配对。用户要求配对、重新连接真机或明确要求由 Codex 操作镜像时使用；不用于普通 App 安装或无关的服务调试。"
---

# Yolo Agent 设备配对

## 用户交互原则

优先检查并直接操作 Mac 上已经配对的 iPhone Mirroring。只要镜像可用，就由 Codex 完成服务发现、配对码生成、手机端输入、权限选择、提交和验收；不要先把服务地址、设备名称或配对码发给用户让其手工填写。

只有出现以下情况时才让用户操作，并只说明当前不可替代的一个动作：

- iPhone Mirroring 尚未与目标 iPhone 配对；
- iPhone 已锁定，必须由用户在手机上解锁或确认；
- Apple 系统明确要求用户本人完成且自动化无法继续的认证。

镜像已经配对但当前未打开，不属于用户阻塞：自行打开并继续。用户明确只要配对信息而不要代操作时，遵从该次请求。

## 工作流

1. 确认目标 Agent、App、LaunchAgent、数据目录和服务身份，不根据旧会话猜测地址、端口、Bundle ID 或设备。
2. 检查 Host 健康状态、现有 iOS 设备授权、Tailscale Serve HTTPS 地址和物理 iPhone 连接状态。
3. 需要新配对码时，先确认没有 `submitting`、`unknown`、`cancel_pending` 等状态不明订单，再只重启目标 Agent Host 以生成新的平台绑定配对码：
   - 不重启 IB Gateway；
   - 不提交、撤销或重发订单；
   - 只读取本次重启后新增的日志片段，避免使用旧的过期码；
   - 等待 Host 恢复健康和交易对账完成后再继续。
4. 启动目标 iOS App 和 iPhone Mirroring。镜像操作的详细方法见 [iPhone Mirroring 操作](references/iphone-mirroring.md)。
5. 在镜像中完成配对：
   - 网络权限选择“无线局域网与蜂窝网络”；
   - 使用 Tailscale Serve 返回的 HTTPS 地址；
   - HTTPS 地址下保持“允许本地开发连接（未加密）”关闭；
   - 直接键盘输入服务地址和配对码，不依赖跨设备剪贴板；
   - 提交后等待进入会话列表或出现明确错误。
6. 同时从手机 UI 和服务端验收：确认出现新的有效 iOS 设备，Agent 身份与目标一致，Host 健康；Trading Agent 还需确认 IBKR 回到 `connected_paper` 且对账完成。

## 安全边界

- 配对码、设备 Token、个人 Tailscale 域名、iPhone UDID 和窗口坐标都是运行时数据，不写入 Skill、仓库、提交信息或长期文档。
- 不撤销旧设备、不清空客户端 Keychain、不删除聊天缓存，除非用户明确要求。
- 不把“重装 App”当作配对失败的默认修复；先区分网络权限、服务地址、设备凭据和 Agent 身份。
- 配对流程不授权交易操作。发现订单状态不明时停止 Host 重启并报告实际阻塞。
- 普通配对不修改产品代码或 Git；只有用户另行要求修复实现时才进入开发流程。
