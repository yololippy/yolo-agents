# YoloTrade Runtime

YoloTrade 私有的 Python Runtime，负责产品身份、聊天能力、IBKR Paper 接入、订单账本、风控入口和券商恢复。IBKR、账户事实和交易规则不得进入共享包。

## 模块

- `product.py`：产品 ID、名称、端口和私有能力声明。
- `ibkr_config.py`：不可变环境配置和安全默认值。
- `ibkr.py`：IBKR SDK 连接、启动读取、行情、What-if、提交、事件和单笔撤单。
- `ibkr_lifecycle.py`：单一 owner loop、自动重连和完整重新对账。
- `paper_order_store.py`：独立 `paper-orders.sqlite3` 账本。
- `paper_orders.py`：草稿、预览、确认、提交、撤单、幂等和恢复状态机。
- `paper_order_broker.py`：订单服务与券商 lifecycle 的严格门面。
- `runtime_tools.py`：模型可用的只读查询、Paper 草稿和单笔撤单工具。
- `api_extension.py`：券商健康和订单 API；写操作只接受有效 Mac 管理设备。

## 启动聊天 Host

默认产品端口为 8765，数据目录为：

```text
~/Library/Application Support/YoloAgents/yolo-trade/
```

独立 ChatGPT Provider：

```sh
PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
  .venv/bin/python -m yolo_trade --mode chatgpt
```

不调用真实模型或 IBKR 的演示模式：

```sh
PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
  .venv/bin/python -m yolo_trade --mode demo
```

显式 Codex 兼容模式使用 Codex 自己管理的登录态，不读取或复制 token：

```sh
PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
  .venv/bin/python -m yolo_trade --mode codex
```

Mac 和 iOS 连接同一 YoloTrade Host，但身份、授权、缓存和 Skills 不与 YoloBeni 共享。旧数据迁移规则见 [架构](../../docs/ARCHITECTURE.md#身份授权与数据隔离)。

## IBKR 配置

环境变量前缀为 `YOLO_AGENTS_TRADING_IBKR_`：

| 字段 | 规则 |
|---|---|
| `ENVIRONMENT` | 必填 `paper` 或 `live`；不根据端口推断。 |
| `HOST` | 默认 `127.0.0.1`，只允许回环地址。 |
| `PORT` | Gateway 默认 Paper 4002、Live 4001，可显式覆盖。 |
| `CLIENT_ID` | 默认 20，必须为非零正整数。 |
| `ACCOUNT` | 显式目标账户，与 Gateway 返回值精确比对。 |
| `READ_ONLY` | 默认 `true`。 |
| `EXECUTION_ENABLED` | 默认 `false`；只有 Paper 且 `READ_ONLY=false` 才允许开启。 |

Live 始终没有执行路径。环境切换不会继承旧账户、地址、端口、client ID 或执行授权。账户号、Gateway 登录凭据、设备 token 和原始上游错误不得进入公开健康、订单响应、日志或文档。

离线配置检查不会打开 socket：

```sh
PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
  .venv/bin/python -m yolo_trade.ibkr --environment paper
```

需要真实 Paper 预检时，必须确认 Gateway 登录的是 Paper、目标账户正确，并由用户决定是否关闭 Gateway 的 `Read-Only API`。Supervisor 不修改该设置。

## 对账与执行门槛

连接后必须核对：

- 精确目标账户；
- 持仓；
- 可见开放订单；
- 已完成订单和当日成交；
- 目标账户摘要。

任一读取失败都会令执行保持关闭。断线会撤销就绪状态；lifecycle 对可恢复连接自动重试，并在重新发布 ready 前执行完整对账。进程重启后的 unknown 主动收敛、外部订单完整账户实测和长期运行仍属于 [ROADMAP](../../docs/ROADMAP.md)。

## Paper 订单

首批仅支持：

- `STK / SMART / USD`；
- `BUY` 或普通 `SELL`；
- 正整数数量；
- `LMT / DAY`；
- `outsideRth=false`。

模型可以读取行情、持仓、开放订单和应用账本，也可以创建不可执行的 Paper 草稿；不能自行确认或提交。提交必须经过 Mac 管理设备的预览、确认和再次明确提交。超时或断开产生 unknown 时绝不自动重发。

完整 HTTP、状态机与幂等规则见 [Paper 订单协议](PAPER_ORDERS.md)。Gateway 登录和恢复见 [运维手册](IBKR_GATEWAY_AUTOMATION.md)。Apple 界面见 [YoloTrade App](../../apps/yolo-trade/README.md)。

## 测试

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
.venv/bin/python -W error::ResourceWarning -m unittest discover \
  -s agents/yolo-trade/runtime-python/tests -v
```

当前测试数量、真实 Gateway 状态和未完成验收只维护在 [ROADMAP](../../docs/ROADMAP.md)。
