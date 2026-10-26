# Mac Agent 与 Apple 客户端通信 v1

这是本项目自己的协议，不传递 PydanticAI 内部对象。首版面向同一位用户的 Mac 和 iPhone；账户与交易服务后续接入。

## 地址、认证与运行模式

- 开发默认 `http://127.0.0.1:8765`。非本机监听必须配置 TLS 证书与私钥；iPhone 访问使用可配置的 HTTPS 地址，客户端不得忽略证书错误或把 token 发给公网 HTTP 地址。
- JSON 编码 UTF-8，字段使用 snake_case，ID 为 UUID，时间为 UTC ISO8601。版本是 URL `/v1` 及健康/配对响应的 `api_version: 1`。
- 除健康检查与首次配对外，每个请求携带 `Authorization: Bearer <device_token>`。令牌在客户端 Keychain 保存，后端只存 SHA-256；无浏览器全局 CORS 放行。
- 服务启动时本机控制台显示两个一次性配对码，分别绑定 `macos` / `ios`，有效十分钟。代码必须与请求平台一致；设备自报平台不能提升配对码授权。
- 两端都有 `chat:read`、`chat:write`；Mac 配对码另授 `devices:manage`，iPhone 不具备管理其他设备的能力。所有交易提交/确认/预授权接口均关闭，即使客户端自造请求也返回 403。
- 健康响应明确 `agent_mode: demo` 或 `pydantic`。演示模式不调用云端模型，不可宣传为真实 AI 或券商连接。

## HTTP 接口

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| `GET /v1/health` | 无认证 | `{status, api_version, server_id, agent_mode, model, trade_execution_enabled:false}` |
| `GET /v1/broker/health` | 已配对设备 | `{broker}`；配置摘要、连接/对账状态及固定只读限制，不含账户标识或凭据 |
| `POST /v1/devices/pair` | `{pairing_code, device_name, platform}` | 201 `{device, token, server_id, api_version}` |
| `GET /v1/devices` | Mac 管理凭据 | `{devices:[device]}` |
| `DELETE /v1/devices/{id}` | 自己或 Mac 管理凭据 | `{device}`；撤销后既有凭据无效 |
| `GET /v1/conversations` | 可选 `query`、`include_archived=true` | `{conversations:[conversation]}` |
| `POST /v1/conversations` | `{title}` | 201 `{conversation}` |
| `PATCH /v1/conversations/{id}` | `{title?, archived?}` | `{conversation}` |
| `GET /v1/conversations/{id}/messages` | 无额外参数 | `{messages:[message]}` |
| `POST /v1/conversations/{id}/messages` | `{client_message_id, content}` | 202 `{message, run_id, status:"accepted"或"duplicate"}` |
| `GET /v1/events` | `after=0&limit=200`，可选 `conversation_id` | `{events, next_cursor, has_more}` |
| `GET /v1/events?stream=true` | 同上；也可 `Accept: text/event-stream` | SSE 事件流，需认证 |

`device={id,name,platform,permissions,created_at,revoked_at}`。

`conversation={id,title,created_at,updated_at,archived}`。

`message={id,conversation_id,role,content,created_at,status,client_message_id,run_id}`。
`role` 为 user/assistant/system；`status` 为 pending/completed/failed。可空字段明确为 null。

消息文本非空、最多 16000 字符，请求体最多 65536 字节。`client_message_id` 由客户端在第一次发送时创建，重试保持不变；相同会话内相同标识和文本返回原请求，不重复运行模型。相同标识但文本不同返回 409。不得把客户端发送的历史、工具调用、权限或确认字段作为服务端事实。

同会话按接收顺序生成回复；不同会话可以并行。发送成功与回复生成成功分开，202 仅代表用户消息已接受。流式过程中失败保留失败状态，不能把已展示的 token 当作保存完毕的最终回复。

## 事件、补齐与断线

事件结构为 `{seq,type,conversation_id,payload,created_at}`。`seq` 是持久化、单调递增的服务端游标，不依赖设备时间。

| type | payload |
| --- | --- |
| `conversation.updated` | `{conversation}` |
| `message.created` | `{message}` |
| `assistant.delta` | `{run_id,delta}` |
| `assistant.completed` | `{run_id,message}` |
| `assistant.failed` | `{run_id,message,error:{code,message}}` |

最终消息和其完成/失败事件在同一事务内保存。`message.created` 与随后 `assistant.completed` 可能引用同一消息，客户端按消息 ID 合并，不能重复显示。

轮询返回 `seq>after` 的事件。只在成功处理事件后推进游标；`has_more=true` 时立即继续取下一页，下一次请求使用返回的 `next_cursor`。同一服务的全局同步使用一个游标；不能把某个会话的过滤游标直接当作全局同步已完成的证明。

SSE 帧示意：

```text
id: 42
event: assistant.delta
data: {"seq":42,"type":"assistant.delta","conversation_id":"…","payload":{"run_id":"…","delta":"你好"},"created_at":"2026-10-24T12:00:00Z"}

```

心跳是 `: heartbeat` 注释，不推进游标。连接约 25 秒主动结束，客户端按最后已处理游标重新连接；无 `after` 参数时支持 `Last-Event-ID`。也可以只实现带游标的增量轮询，得到相同的业务结果。SSE 在每次扫描前重新检查设备撤销状态；撤销后关闭既有流。

初次连接、换服务端 `server_id`、重新配对时，隔离或重置本地服务缓存/游标；恢复连接后以服务端记录为准。设备离线时浏览缓存，发送失败保持原幂等标识，不伪造“已发送”。

## 错误与恢复边界

错误统一 `{error:{code,message}}`；400 参数错误、401 未认证/撤销/无效配对码、403 权限/交易禁用、404 不存在、409 幂等或归档冲突、413 超过请求体限制、415 非 JSON、429 配对限流、500 服务错误。

错误不返回模型原始异常、账户信息、密钥或服务器文件路径。配对按来源 IP 每分钟最多五次尝试。

已接受但无产品最终回复的运行，重启时先检查 Runtime 是否已保存匹配该输入的完整回复；确定有完整缓存时直接补齐产品结果，不再调用模型。其余标为 `run_interrupted`，不自动重跑模型或工具。当前没有交易工具；未来交易工具必须另行实现授权、幂等和券商对账，不能借聊天重试直接执行。

本协议已定义设备认证、增量补齐和 HTTPS 接口；公网可达性、可信证书配置、iOS 后台推送及真机测试分别验收，不能仅凭本机 HTTP 测试认定完成。
