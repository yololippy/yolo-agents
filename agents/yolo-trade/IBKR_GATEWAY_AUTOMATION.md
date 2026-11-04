# IBKR Gateway 自动恢复（Paper）

更新日期：2026-11-03

## 边界

本流程只负责在 macOS 上启动和恢复 **IB Gateway Paper Trading** 登录界面，不负责交易判断、订单提交或券商 API 对账。

- `Gateway Supervisor` 检测 `127.0.0.1:4002`、Gateway 进程、IBC 安装和 Keychain 凭据。
- Gateway 进程不存在时，Supervisor 才通过 IBC 启动它。
- Gateway 进程存在但 4002 未监听时，状态为 `authentication_required`，提醒用户检查登录窗口或在 IBKR Mobile 完成 2FA；不会循环结束进程。
- 4002 恢复后，现有 `IBKRBrokerLifecycle` 自动重连并完整对账；Supervisor 不调用 TWS API。
- Gateway 与 IBKR 之间的 `1100`／`2110` 网络中断不触发进程重启。
- Live 模式没有入口；IBC 配置固定 `TradingMode=paper` 和 `ReadOnlyLogin=no`。

## 凭据与本机文件

用户名和密码分别保存在 macOS Keychain：

- `com.yolo-agents.ibkr.paper.username`
- `com.yolo-agents.ibkr.paper.password`

IBC 启动前，Supervisor 从 Keychain 读取两项，在 `~/Library/Caches/YoloAgents/ibkr-gateway/config.runtime.ini` 原子生成权限为 `0600` 的运行配置。凭据不会进入命令行、环境变量、仓库、应用数据库或 Supervisor 状态文件。IBC 诊断输出经过流式过滤，实际凭据和 `IbLoginId`／`IbPassword`／FIX 凭据行会在落盘前替换为 `<redacted>`。

运行配置在 IBC 存活期间保留，以支持 IBC 的每日自动重启。Mac 用户目录应启用 FileVault；登录 Keychain 未解锁时自动启动会保持失败关闭。

## 安装与启用

```bash
cd /Users/yololippy/project/yolo-agents
bin/install-ibc-macos
bin/yolo-ibkr-gateway credentials-set
bin/yolo-ibkr-gateway install-launch-agent
bin/yolo-ibkr-gateway status
```

`credentials-set` 必须在交互式终端执行。命令会依次以隐藏输入方式读取用户名、密码和密码确认，再通过 macOS `Security.framework` 直接写入 Keychain；不会再出现 `security` CLI 的额外输入提示。不要把凭据附加在命令后。

LaunchAgent 安装到：

```text
~/Library/LaunchAgents/com.yolo-agents.ibkr-gateway.plist
```

日志与脱敏状态分别位于：

```text
~/Library/Logs/YoloAgents/ibkr-gateway/
~/Library/Application Support/YoloAgents/ibkr-gateway/status.json
```

## IBC 与 Gateway 设置

生成配置采用以下安全默认值：

- `AutoRestartTime=06:15 AM`：每天在 Asia/Shanghai 的美股常规时段之外自动重启。
- `ColdRestartTime=14:15`：每周日在 IBKR 美东 01:00 令牌失效后发起完整登录，届时通常需要手机 2FA。
- `AcceptIncomingConnectionAction=reject`：只使用 Gateway 中已经配置的 localhost/Trusted IP。
- `ReadOnlyApi=`：不由 IBC 修改 Gateway 的 API 读写设置。
- `ExistingSessionDetectedAction=manual`：竞争登录时不自动接管或结束其他会话。
- `ReloginAfterSecondFactorAuthenticationTimeout=no`：防止无人响应时循环发送 2FA 请求。

Gateway 本身仍需确认：

1. `Configure → Settings → API → Settings` 的 Socket port 为 `4002`。
2. Trusted IP 只允许 `127.0.0.1`／localhost。
3. 是否取消 `Read-Only API` 由 Paper 下单验收单独决定，Supervisor 不修改它。
4. 不要使用相同 IBKR 用户名同时登录其他交易应用；竞争会话需人工处理。

不要启用 `Maintain and resubmit orders when connection is restored`，当前系统会在恢复后对账，并对未知订单保持失败关闭，绝不自动重发。

## 状态含义

| 状态 | 含义与处理 |
| --- | --- |
| `gateway_ready` | 4002 可连接；交易服务随后重新对账。 |
| `authentication_required` | Gateway/IBC 仍在，但 API 端口未开放；检查 Mac 登录窗口和 IBKR Mobile。 |
| `credentials_missing` | 尚未在 Keychain 保存两项 Paper 凭据。 |
| `ibc_missing` | Keychain 已准备，但固定版本 IBC 尚未安装。 |
| `gateway_stopped` | 条件齐全且 Gateway 不在；Supervisor 会按退避启动 IBC。 |

如果用户无法完成 2FA，系统保持不可执行；定时订单不得绕过认证或在过期后补发。

## 本机验收（2026-11-03）

- 固定版本 IBC 3.24.2 已通过 SHA-256 校验并安装。
- 使用 Keychain 凭据结束手工 Gateway 后，Supervisor 状态实际经过 `gateway_stopped → authentication_required → gateway_ready`，IBC 自动填充并登录 Paper，4002 恢复监听。
- Trading Host 实际经过不可用、连接和重新对账，最终回到 `connected_paper`、`ready=true`、`data_complete=true`；账户和权限验证通过，受跟踪订单为 0。
- 连续测试冷启动曾触发 IBKR 旧会话的 `Re-login is required`；Supervisor 被人工暂停，等待服务端释放后单次重试成功。正式运行不得用循环冷启动测试认证。
- IBC 会在诊断输出中打印配置；当前使用流式过滤器，在落盘前同时替换凭据字段和实际 Keychain 值。验收扫描中实际用户名、密码出现次数均为 0。
- 每日 Auto Restart、主机重启和真实跨周 Sunday Cold Restart 仍需按自然时间继续观察，不能由本次连续冷启动代替。

## 停用与回滚

以下操作只卸载本流程，不结束已经运行的 Gateway，也不删除订单或聊天数据：

```bash
bin/yolo-ibkr-gateway uninstall-launch-agent
bin/yolo-ibkr-gateway credentials-delete
```

第一条停止并移除用户级 Supervisor；第二条删除两项 Keychain 凭据及缓存中的明文运行配置。IBC 程序目录可保留，便于核对和回滚版本。
