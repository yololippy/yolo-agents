# IBKR Paper 交易 Agent

更新日期：2026-10-28。

本模块是 `trading-agent` 私有的 IBKR 运行时。它提供显式 paper/live 配置、Gateway 生命周期、启动对账、Paper 股票限价单和独立订单账本。Live 环境没有执行路径；聊天模型也没有直接订单工具。Mac 管理设备必须按“草稿 → What-if → 确认 → 提交”逐步操作，iOS 仅查看脱敏订单状态。

## 模块

- `ibkr_config.py`：不可变配置、来源审计和安全默认值。
- `ibkr.py`：同一 asyncio owner 线程上的 SDK 连接、启动读取、合约确认、What-if、提交、事件与单笔撤单。
- `ibkr_lifecycle.py`：Host 后台生命周期及线程安全同步门面。
- `paper_order_store.py`：独立 `paper-orders.sqlite3` schema、事务和请求/事件存储。
- `paper_orders.py`：草稿、预览、确认、提交、撤单、幂等和恢复状态机。
- `paper_order_broker.py`：订单服务与券商 lifecycle 之间的严格边界。
- `api_extension.py`：`/v1/broker/health` 和 `/v1/orders` 路由；写操作仅接受有效 Mac 管理设备。

账户号、Gateway 登录凭据、设备 token 和上游原始错误不写入公开健康、订单响应或文档。配置目标通过不可逆 `config_id` 暴露给客户端。

## 配置与保护

| 字段 | 规则 |
| --- | --- |
| `environment` | 必填 `paper` 或 `live`；不根据端口推断。 |
| `host` | 默认 `127.0.0.1`，仅允许回环地址。 |
| `port` | Gateway 默认 Paper 4002、Live 4001；允许显式覆盖。 |
| `client_id` | 默认 20，必须为非零正整数；不自动绑定手工订单。 |
| `account` | 连接前显式指定，并与 `managedAccounts()` 精确比对；不取第一个账户。 |
| `read_only` | 默认 `true`。只有 Paper 且同时显式开启 execution 时允许 `false`。 |
| `execution_enabled` | 默认 `false`；只有 Paper＋`read_only=false` 可开启，Live 永远拒绝。 |
| `connect_timeout` | 默认 5 秒，范围 `(0, 60]`。 |

环境变量前缀为 `YOLO_AGENTS_TRADING_IBKR_`。布尔值只接受 `true`/`false`。环境切换会丢弃旧环境的账户、地址、端口、client ID 和执行授权，避免 Paper 配置泄漏到 Live。

Paper 执行还要求目标账户由 Gateway 返回，并属于当前明确支持的 Paper 账户命名空间。IBKR 没有公开账户号命名契约，当前仅对白名单 `DU+数字` 与实机返回的 `DUR+数字` 放行，未知命名空间失败关闭。账户前缀不是唯一依据：显式环境、精确目标账户、Gateway 返回账户、完整启动读取及当前会话必须同时匹配。

离线检查不连接 Gateway：

```sh
PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python .venv/bin/python -m yolo_trading.ibkr --environment paper
PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python .venv/bin/python -m yolo_trading.ibkr --environment live --port 4002
```

## 启动

用户名、密码和二次认证只在 IB Gateway 内完成，不能写进仓库或 Agent 配置。只读观察时保留 Gateway 的 `Read-Only API`，并使用默认 `read_only=true` / `execution_enabled=false`。需要验证 Paper 订单时，必须先确认 Gateway 登录的是 Paper，再取消 Gateway 的 `Read-Only API`；否则开放订单/已完成订单读取会被拒绝，启动对账不会就绪。

示例只展示变量名，不保存实际账户号：

```sh
export YOLO_AGENTS_TRADING_IBKR_ENVIRONMENT=paper
export YOLO_AGENTS_TRADING_IBKR_HOST=127.0.0.1
export YOLO_AGENTS_TRADING_IBKR_PORT=4002
export YOLO_AGENTS_TRADING_IBKR_CLIENT_ID=20
export YOLO_AGENTS_TRADING_IBKR_ACCOUNT='在本机终端填写实际 Paper 账户'
export YOLO_AGENTS_TRADING_IBKR_READ_ONLY=false
export YOLO_AGENTS_TRADING_IBKR_EXECUTION_ENABLED=true

PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python \
  .venv/bin/python -m yolo_trading --mode codex
```

构造、离线检查和 `health()` 不打开 socket。Host 只在显式配置后启动 lifecycle；失败不影响聊天服务。正常退出、SIGTERM 和异常路径都会尝试在 owner 线程断开 SDK，不静默重试订单。

## 启动对账与健康

连接成功后必须完成以下读取，任何一项失败都令 `data_complete=false` 并阻止订单：

- 精确目标账户；
- 持仓；
- 可见开放订单；
- 当日成交；
- 目标账户摘要。

健康状态包含 `connected`、`account_verified`、`environment_verified`、`data_complete`、`execution_enabled`、会话 ID 和逐项 reconciliation，不包含账户号。只有显式 Paper 可写配置、受支持账户、当前连接和五项启动读取全部通过时才进入 `connected_paper`。

端口可连不等于环境或权限已验证。断线、会话冲突和券商恢复会撤销就绪状态；未重新对账前禁止操作。当前没有自动重连/跨重启主动券商对账调度，相关恢复仍是后续任务。

## Paper 订单范围

首批只支持：

- `STK / SMART / USD`；
- `BUY` 或普通 `SELL`；
- 正整数数量；
- `LMT / DAY`；
- `outsideRth=false`。

预览要求唯一、完整合约，执行 IBKR What-if 并返回保证金/佣金估计，不提交实单。提交绑定不可变草稿、预览、确认、`config_id`、`session_id` 和稳定 `orderRef`；数据库先写 `submitting`，SDK 最多调用一次。超时、断开或无法判断是否已接单时标记 `unknown`，不自动重发，并阻止相同经济意图绕过。

撤单只允许当前 Agent 所属、已知 broker ID 的单笔活动订单，不调用 global cancel。部分成交、迟到成交、拒单、撤单竞争和乱序事件采用单调状态；完整成交优先于迟到撤单确认。

HTTP 和客户端契约见 [通信协议](../../packages/protocol/COMMUNICATION_V1.md)。Mac/iOS 界面说明见 [Apple 客户端](../../apps/trading-agent/README.md)。

## 当前验证边界

已完成：

- 交易 Agent 111 项 Python 测试；其中 Paper 执行、HTTP、持久化和恢复均使用注入式内存 SDK/本机测试服务；
- 共享 Swift SDK 43 项中的 42 项通过、1 项真实服务测试默认跳过；共享 SwiftUI 17 项、Trading Agent 客户端 21 项通过；
- Mac arm64 与 iOS Simulator Debug 构建；
- 本机 Gateway 已安装、Paper 登录及端口可达；现有只读 Agent 连接曾成功。

未完成：

- Gateway 的 `Read-Only API` 仍开启，Paper 可写连接预检被官方接口拒绝；
- 尚未对真实 Gateway 执行 What-if、提交模拟订单、成交或撤单；
- 未做进程重启后的活动订单主动恢复、外部订单完整对账、跨周认证和长期运行验收；
- 未开放 Live，也没有实盘验收。

运行测试：

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python \
.venv/bin/python -W error::ResourceWarning -m unittest discover \
  -s agents/trading-agent/runtime-python/tests -v
```

资料：[IBKR Paper Trading](https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/)、[TWS API 连接](https://www.interactivebrokers.com/docs/tws-api/doc/connectivity/verify-api-connection)、[ib_async API](https://ib-api-reloaded.github.io/ib_async/api.html)。

Paper HTTP 请求、状态机与幂等约定见 [订单协议](PAPER_ORDERS.md)；双端专用 Swift 实现见 [Trading Agent 客户端](../../apps/trading-agent/client-swift/README.md)。
