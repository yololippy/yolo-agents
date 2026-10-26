# IBKR 第一阶段：配置、只读连接与健康状态

更新日期：2026-10-25。

本阶段提供独立模块，供 main 接入聊天服务。未修改聊天 API、认证、编排或主入口；没有下单/撤单接口，也没有运行账户连接、安装 IB Gateway 或保存登录凭据。需求依据见 [项目大纲](../docs/PROJECT_OUTLINE.md) 和 [任务清单](../docs/TODO.md)。

## 模块与调用

- `trading_agent.ibkr_config.IBKRConfig`：不可变配置，校验类型、环境、本机地址、端口、client ID 和只读限制。
- `load_ibkr_config(base=None, environ=None, overrides=None)`：配置合并，优先级为默认值 < 基础字典/JSON < 环境变量 < 显式覆盖。
- `trading_agent.ibkr.IBKRAdapter(config)`：构造和 `health()` 不导入 SDK、不探测端口、不建立连接。由未来集成方显式调用异步 `connect()`、`disconnect()`、`reconfigure(config)`。

`reconfigure` 在生命周期锁下先关闭旧连接再应用配置，返回新健康状态而不自动连接。不能确认原连接关闭时返回 `disconnect_failed`，保留旧配置，阻止切换。健康输出包含配置摘要、字段来源、配置指纹、修订号及最近一次切换的前后指纹；集成方负责将这些信息写入已有审计存储，本模块不提供持久审计账本。账户 ID 不出现在健康输出或配置 repr 中。

## 显式配置与保护

| 字段 | 规则 |
| --- | --- |
| `environment` | 必填 `paper` 或 `live`，不按端口或账户前缀推断。 |
| `host` | 默认 `127.0.0.1`；首版仅支持回环 IP 或规范化后的 `localhost`，不解析任意域名。 |
| `port` | 未指定时按显式环境取默认 Gateway 端口：paper 4002、live 4001；允许显式自定义，不因此更改环境。 |
| `client_id` | 默认 20，范围 1～2147483647；拒绝 0，避免库自动绑定手工订单。 |
| `read_only` | 默认且必须为 true。 |
| `execution_enabled` | 默认且必须为 false；paper/live 都拒绝开启，本阶段不支持执行。 |
| `account` | 可缺省以检查配置；连接前必须显式指定并与 Gateway 返回的 managed accounts 比对，不自动取第一个账户。 |
| `connect_timeout` | 默认 5 秒，范围大于 0 且不超过 60 秒，限制一次完整连接尝试。 |

环境变量使用 `TRADING_AGENT_IBKR_` 前缀，例如 `TRADING_AGENT_IBKR_ENVIRONMENT`、`TRADING_AGENT_IBKR_PORT`、`TRADING_AGENT_IBKR_ACCOUNT`。布尔值只接受 true/false；未知字段、用户名或密码字段均拒绝，错误不回显原始输入。

某一覆盖层改变 environment 时，先丢弃低优先级的整个旧配置，再应用该层显式字段和安全默认值。因此 paper → live 不会继承旧 account、host、port 或 client ID；同一环境的覆盖则保留其他字段。

SDK `readonly=True` 主要控制库的启动同步行为，不能证明 Gateway 的 Read-Only API 设置已启用。用户仍需在 Gateway 开启只读。本模块不暴露底层 client 或交易方法，也不将本机连接、目标账户匹配当作实际 paper/live 环境已经验证。

## 离线检查

在仓库根目录运行以下命令，只检查配置和依赖，绝不连接：

```sh
PYTHONPATH=server .venv/bin/python -m trading_agent.ibkr --environment paper
PYTHONPATH=server .venv/bin/python -m trading_agent.ibkr --environment live --port 4002
```

第二条仍然明确表示 live 配置；端口 4002 不会将它变成 paper。亦可使用 `--config /path/to/ibkr.json`，再用同名 CLI 参数覆盖字段。不要在配置中填写 Gateway 登录用户名或密码。

退出码：0 表示配置完整且依赖可发现（不代表 Gateway 可用）；1 表示缺依赖或目标账户等配置未补齐；2 表示配置不合法/文件无法解析。结果为 JSON，含固定错误代码及可解释提示。

可选依赖位于 [requirements-ibkr.txt](requirements-ibkr.txt)，已安装到共享 `.venv` 并固定在 [requirements.lock](requirements.lock)。其他聊天/演示模式不依赖它；只有显式配置 IBKR 环境时才会构造适配层。

## 健康状态语义

- `dependency_missing` / `dependency_unavailable`：未安装或不能加载 ib_async。
- `configuration_incomplete`：如目标账户未配置，不发起连接。
- `disconnected`：未连接；`connection_attempted=false` 时没有检查 Gateway，不能宣称它已停止或认证过期。
- `connecting` / `connected_read_only`：SDK 正在连接或已完成启动握手，不表示应用级账户/挂单/成交对账完成。
- `gateway_unavailable`：显式连接失败、SDK 断开或连接已丢失；错误不足以判断究竟是未启动、未登录、端口错误还是认证失效，需在 Gateway 确认。
- `broker_unavailable` / `session_conflict`：收到对应券商连接或竞争会话事件。
- `reconciliation_required`：连接恢复，仍需重新核对状态和订阅。
- `account_mismatch`：未返回目标账户，已断开，不自动改用其他账户。
- `disconnect_failed`：旧连接关闭无法确认，配置切换被阻止。

第一阶段所有健康结果固定 `execution_enabled=false`、`environment_verified=false`、`data_complete=false`。没有账户数据快照、完整订单对账或自动重连调度；这些由下一阶段实现，不能把 `connected_read_only` 解释为可交易。

## 验证与后续配置

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server .venv/bin/python -W error::ResourceWarning -m unittest discover -s server/tests -p 'test_ibkr*.py' -v
```

测试仅使用模拟客户端，覆盖配置优先级/审计、环境切换重置、live 和 paper 执行禁用、无 SDK/账户、精确连接参数、无 Gateway 故障、账户不匹配、超时/取消、并发连接、旧连接关闭失败及券商连接恢复状态。测试通过不代表真实 SDK 或账户已联调。

用户后续需在 Mac 安装/登录 IB Gateway，选择环境，开启 Read-Only API 并核对仅本机连接、实际端口、可用 client ID、目标账户及行情订阅。程序不接收登录密码；首次登录/每周认证仍在 Gateway 与 IB Key 中完成。main 已将配置摘要接入聊天服务的健康状态；真实只读连接与对账仍须单独验收。

接口依据：[ib_async API 文档](https://ib-api-reloaded.github.io/ib_async/api.html)、[IBKR 连接握手说明](https://www.interactivebrokers.com/docs/tws-api/doc/connectivity/verify-api-connection)。
