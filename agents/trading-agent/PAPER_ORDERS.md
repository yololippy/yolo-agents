# Paper 订单 v1

接口使用共享 [通信协议](../../packages/protocol/COMMUNICATION_V1.md) 的设备认证与错误格式。

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| `GET /v1/orders` | 已配对设备；无查询参数 | `{orders:[order]}`；最多最近 100 条脱敏订单 |
| `GET /v1/orders/{id}` | 已配对设备；无查询参数 | `{order,events:[{seq,type,state,error_code,created_at}]}`；最多 500 条订单事件 |
| `POST /v1/orders` | Mac 管理凭据；草稿请求，见下文 | 201 `{order}`；相同请求重试返回原响应 |
| `POST /v1/orders/{id}/preview` | Mac；`{client_request_id,version,content_hash}` | `{order}`；qualify＋what-if，成功为 proposed |
| `POST /v1/orders/{id}/confirm` | Mac；绑定版本、hash、preview、期限和明确动作 | `{order}`；confirmed，不自动提交 |
| `POST /v1/orders/{id}/submit` | 同一确认 Mac；绑定 confirmation_id 与明确动作 | `{order}`；先落库 submitting，再调用券商一次 |
| `POST /v1/orders/{id}/cancel` | Mac；绑定版本、hash 和明确撤单动作 | `{order}`；先落库 cancel_pending，只撤本单 |

仅允许股票 `STK`、`SMART`、`USD`、`LMT`、`DAY`，不含盘外、修改、批量取消、定时单或 Live。数量为 JSON 整数 1–100000，拒绝 bool/小数；限价为正十进制**字符串**，不超过 1000000，至多四位小数，不接受指数、NaN/Infinity 或浮点价格。范围是输入边界，不替代券商资金、合约最小变动价位和交易权限检查。

创建草稿的完整字段：

```json
{
  "client_request_id": "b651793e-0e7e-41c8-a628-b602313d1561",
  "account_selection": "服务端 broker.configuration.config_id 的 64 位小写十六进制值",
  "contract": {"symbol":"AAPL","sec_type":"STK","exchange":"SMART","currency":"USD"},
  "side": "BUY",
  "quantity": 2,
  "limit_price": "10.00",
  "order_type": "LMT",
  "tif": "DAY"
}
```

account_selection 是当前配置目标的不透明 ID，不接受账户号，不把请求当作账户授权。服务端核对配置为 paper、目标存在且 ID 完全匹配；没有配置目标时不能创建草稿。草稿不要求连接可执行，但预览、确认、提交、撤单必须再次核验完整门面快照：paper、execution_enabled=true、read_only=false、connected/data_complete/account_verified/permissions_verified/environment_verified=true，health_stale=false，config_id 和 session_id 有效。门面缺失或任何值未知均拒绝，Live 无执行路径。

流程为 `draft -> previewing -> proposed -> confirmed -> submitting -> accepted/working/partial/filled/rejected/unknown`；活动订单可进入 `cancel_pending -> cancelled/filled/unknown`。previewing、submitting、cancel_pending 是先提交数据库的中间状态。预览只有在唯一合约匹配且 what-if 完整、accepted 为 true、USD 保证金与佣金估计有效后成功；what-if 不是实单提交，也不保证后续成交。[IBKR 官方 what-if 与订单回报](https://interactivebrokers.github.io/tws-api/order_submission.html)。

成功预览增加 version，并生成新的 content_hash、preview.id、expires_at（服务端 UTC，120 秒）。hash 绑定规范化订单、配置目标、会话身份、合约 con_id 和完整预览。重复预览会使旧版本失效；确认后不再允许更改或重新预览，过期时新建草稿。客户端必须显示最新完整订单与预览，只有用户明确动作才发确认：

```text
confirm = {client_request_id,version,content_hash,preview_id,expires_at,user_action:"confirm_paper_order"}
submit  = {client_request_id,version,content_hash,confirmation_id,user_action:"submit_paper_order"}
cancel  = {client_request_id,version,content_hash,user_action:"cancel_paper_order"}
```

每次操作使用自己的 UUID client_request_id。确认记录唯一绑定 order/version、hash、preview、有效期、服务端用户与配对设备及明确动作；submit 必须来自同一确认设备。读取 JSON 后和实际调用门面前重新认证，撤销设备不能继续操作。这里的动作字段是客户端操作契约，不能用模型声称“用户批准”替代；聊天工具未注册这些入口，也不持有设备凭据。

同一个 client_request_id 的操作、内容、设备和用户相同时重放原 HTTP 状态与响应；不同内容或设备返回 409 idempotency_conflict。在途重复请求返回 202 和当前订单，绝不再次外呼。同一确认版本不能再次提交。所有订单意图、不可变版本、确认、请求、错误码和状态事件保存在独立 `paper-orders.sqlite3`，不把官方模型状态或聊天表当作订单账本。请求只保存正文 hash，拒绝原始错误、账户号或凭据进入日志/公开视图。已识别请求的领域错误也记入请求和事件；无有效身份或无法解析的 HTTP/JSON 在订单领域之前拒绝。

调用 submit 前先原子保存请求和 submitting，commit 后重新检查门槛、期限、身份，再通过 lifecycle 调用一次。超时、断开、无效回报及无法判断是否已提交时保留 unknown，绝不以异常推断“未发单”。unknown 或其他活动状态会阻止相同经济意图重新创建/提交，即使换 client_request_id 或配置 ID 也不会绕过；这是跨配置目标的保守阻断，需先对旧单取得确定对账结果。已提交、在途和不确定请求没有自动重试路径。

启动后的首次订单访问在 Host 单实例锁内恢复账本：未完成 submitting/cancel_pending 转 unknown，未完成 previewing 回到 draft；未完成请求保存恢复结果，不重放券商调用。schema 0（空库）升级到 1，未来版本拒绝启动，不降级成可执行状态。GET 列表/详情返回持久化状态，并可从 lifecycle 的 `orders_snapshot()` **只读缓存**同步已知 orderRef 且 config_id/session_id 完全对应的回报，不发起新的 SDK 请求；无变化不重复记事件。客户端不能上传券商回报。主动对账也可由受信 lifecycle 调用 `PaperOrderService.apply_broker_update(order_ref, normalized_result)`，必须先匹配目标账户、orderRef、客户端归属和 broker order ID。

撤单只适用于已知 broker ID 的 working/accepted/partial 本单。unknown 不允许盲目再发撤单；pending 期间重复撤单返回已有状态。cancel_pending 只表示已请求，不能显示为已撤销；成交数量只增不减，完整成交优先于迟到的撤单确认。[IBKR 官方撤单状态](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/order-status)。

公开 order 包含本地 id、原始创建请求 ID、environment:paper、规范化草稿字段、version/content_hash/state、preview、confirmation、filled_quantity、average_fill_price、error_code 和 UTC 时间。preview 只含 id/qualified_contract/what_if/expires_at，confirmation 只含 id/version/content_hash/expires_at；不回显账户号、设备令牌、内部 broker ID、原始 SDK 对象或异常。

### 与券商 lifecycle 对齐的唯一门面

定义在 `agents/trading-agent/runtime-python/yolo_trading/paper_order_broker.py`，全部为线程安全的同步门面；SDK 工作只在 lifecycle 自己的 event loop 执行：

- `paper_order_snapshot()`：上述执行门槛的扁平快照，含不透明 config_id/session_id。
- `preview_paper_order(intent, *, order_ref, context)`：只做 qualify 和 what-if；返回 qualified_contract（含正整数 con_id）与 what_if（complete/accepted/currency/initial_margin_change/maintenance_margin_change/commission，金额为十进制文本）。
- `submit_paper_order(intent, *, order_ref, preview, context)`：检查 owner loop 上的最新配置、会话、账户、合约、权限和数据，再调用一次。相同 order_ref 不得在门面内部重试。
- `cancel_paper_order(*, order_ref, broker_order_id, context)`：只撤己方关联本单，不调用 global cancel。

submit/cancel 回报必须含匹配本地 UUID 的 order_ref，以及规范化 status、内部 broker_order_id（字符串）、filled_quantity（整数）、可选 average_fill_price（十进制文本）。未知/错误不返回账户或上游文字。订单服务不会直接导入 SDK；当前门面未全部实现时，预览/执行保持阻断，健康响应也不会宣布执行已启用。
