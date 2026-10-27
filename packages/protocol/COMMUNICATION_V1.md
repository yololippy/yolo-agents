# Mac Agent 与 Apple 客户端通信 v1

这是本项目自己的协议，不传递 PydanticAI 内部对象。首版面向同一位用户的 Mac 和 iPhone；账户与交易服务后续接入。

## 地址、认证与运行模式

- 开发默认 `http://127.0.0.1:8765`。非本机监听必须配置 TLS 证书与私钥；iPhone 访问使用可配置的 HTTPS 地址，客户端不得忽略证书错误或把 token 发给公网 HTTP 地址。
- JSON 编码 UTF-8，字段使用 snake_case，ID 为 UUID，时间为 UTC ISO8601。版本是 URL `/v1` 及健康/配对响应的 `api_version: 1`。
- 除健康检查与首次配对外，每个请求携带 `Authorization: Bearer <device_token>`。令牌在客户端 Keychain 保存，后端只存 SHA-256；无浏览器全局 CORS 放行。
- 服务启动时本机控制台显示两个一次性配对码，分别绑定 `macos` / `ios`，有效十分钟。代码必须与请求平台一致；设备自报平台不能提升配对码授权。
- 两端都有 `chat:read`、`chat:write`；Mac 配对码另授 `devices:manage`，iPhone 不具备管理其他设备的能力。所有交易提交/确认/预授权接口均关闭，即使客户端自造请求也返回 403。
- 健康响应明确 `agent_mode: demo`、`pydantic` 或 `codex`。演示模式不调用云端模型；Codex 模式复用 Mac 上由 Codex 受管的 ChatGPT 登录，不向设备提供 OpenAI token。

## HTTP 接口

| 方法与路径 | 请求 | 响应 |
| --- | --- | --- |
| `GET /v1/health` | 无认证 | `{status, api_version, server_id, agent_mode, model, trade_execution_enabled:false}` |
| `GET /v1/models` | 已配对设备 | `{models:[model],default_model,catalog_only:true,auth:{status,method},error?}` |
| `GET /v1/codex/account` | Mac 管理凭据；Codex 模式 | `{account:{status,email,plan_type}}` |
| `POST /v1/codex/account/login` | Mac 管理凭据；空 JSON `{}` | `{login_id,auth_url}`；启动 Codex 托管浏览器登录 |
| `POST /v1/codex/account/logout` | Mac 管理凭据；空 JSON `{}` | `{account:{status,email,plan_type}}`；退出后重新读取实际状态 |
| `GET /v1/broker/health` | 已配对设备 | `{broker}`；配置摘要、连接/对账状态及固定只读限制，不含账户标识或凭据 |
| `POST /v1/devices/pair` | `{pairing_code, device_name, platform}` | 201 `{device, token, server_id, api_version}` |
| `GET /v1/devices` | Mac 管理凭据 | `{devices:[device]}` |
| `DELETE /v1/devices/{id}` | 自己或 Mac 管理凭据 | `{device}`；撤销后既有凭据无效 |
| `GET /v1/conversations` | 可选 `query`、`include_archived=true` | `{conversations:[conversation]}` |
| `POST /v1/conversations` | `{title,model?:string或null}` | 201 `{conversation}` |
| `PATCH /v1/conversations/{id}` | `{title?, archived?, model?:string或null}` | `{conversation}` |
| `GET /v1/conversations/{id}/messages` | 无额外参数 | `{messages:[message]}` |
| `POST /v1/conversations/{id}/messages` | `{client_message_id, content}` | 202 `{message, run_id, status:"accepted"或"duplicate"}` |
| `GET /v1/events` | `after=0&limit=200`，可选 `conversation_id` | `{events, next_cursor, has_more}` |
| `GET /v1/events?stream=true` | 同上；也可 `Accept: text/event-stream` | SSE 事件流，需认证 |

`device={id,name,platform,permissions,created_at,revoked_at}`。

`conversation={id,title,created_at,updated_at,archived,model}`；`model` 可空，null 表示使用服务端默认模型。旧数据库升级后该字段默认 null，不删除消息或重置事件游标。

`model={id,display_name,description,is_default}`，ID 作为不透明字符串使用。`default_model` 可空。模型目录只是 catalog，不证明当前账户具有某个模型的 inference entitlement；只有真实 `turn/completed` 状态为 completed 才证明该次调用成功。

模型目录的 `auth.status` 为 `authenticated`（Codex 检测到受管 ChatGPT 登录）、`login_required`、`unavailable`、`not_required`（demo）或 `configured`（Pydantic 的供应商配置，尚不证明密钥有效）。`auth.method` 为 `chatgpt`、`api_key` 或 null。模型目录响应不含邮箱、账户 ID、OAuth token、API key、登录 URL 或服务器目录。产品 API 不接受或转发 OpenAI token，也不静默回退 demo。

### Codex 账号管理

三个账号接口同时要求配对记录中的 `platform:macos` 和 `devices:manage`。iOS、只有聊天权限的 Mac 均返回 403；设备身份和权限来自服务端配对记录，不能由请求自报。POST 读取请求体后再次核验凭据及权限，撤销的设备不能继续提交。demo/pydantic 模式返回 409 `codex_not_enabled`。

账号响应只含 `status`、可空 `email` 和可空 `plan_type`。`status` 为 `authenticated`、`login_required` 或 `login_pending`。仅 ChatGPT 受管账号视为 authenticated；其他认证方式返回 login_required 且邮箱、套餐为 null。底层固定调用 `account/read`（`refreshToken:false`），不从模型目录推测登录，不传出完整账户对象。

登录请求不接受客户端字段，固定调用 `account/login/start`，参数为 `type:chatgpt`、`useHostedLoginSuccessPage:true`、`appBrand:chatgpt`。只返回经校验的 UUID `login_id` 和 HTTPS `auth_url`，允许的登录主机为 `auth.openai.com`、`chatgpt.com`；拒绝用户信息、异常端口、片段和凭据查询字段。Mac 客户端在本机浏览器打开该 URL，Codex app-server 持有本地回调及凭据。登录 URL 不写入聊天记录、事件或应用日志。

客户端轮询 `GET /v1/codex/account` 获取后续状态；启动成功不代表登录完成。收到 `account/login/completed` 后结束 login_pending，失败时不转发上游错误原文；进程丢失也会结束该进程的 pending 标记。已有 pending 登录时再次启动返回 409 `codex_busy`。pending 期间新消息不启动推理，产生 `assistant.failed` / `login_required`；用户完成登录后需发送新消息，不自动重试旧输入。

退出固定调用 `account/logout`，随后读取状态；不读取、返回或删除应用自己解析的凭据文件。登录和退出对同一 Mac 的 Codex 受管存储生效，不是单个产品会话的登录。退出不会撤销产品设备 token 或删除聊天。改变登录账号后，既有 thread 的账号指纹校验仍生效，应新建产品会话。

服务中任一会话存在已接受、排队或正在生成的回复时，登录和退出返回 409 `codex_busy`，不发出账号变更 RPC。账号变更与消息接受互斥；Runtime 还独立保护直接调用和登录回调等待窗口。这些保护覆盖本服务的运行，不能锁住另一个 Codex 客户端发起的登录或退出。上游不可用或结果不确定时返回 503 `codex_unavailable`，不自动重放登录/退出；先重新查询状态再由用户决定下一步。

模型选择持久化在会话上。PATCH 未提供 model 时保持原值，显式 null 恢复默认。存在已接受/排队运行时切换模型返回 409，待回复完成后再改。Codex 下一轮遇到模型变化会新建受限 thread，携带最多约 64,000 字符的已完成用户/助手文本作为引用上下文，发出 `agent.thread.rebuilt`；产品消息和事件记录完整保留，不重放旧用户请求或工具。缺少上下文的部分仍可在产品历史中浏览，不承诺所有旧消息同时进入模型上下文。

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
| `agent.thread.rebuilt` | `{reason:"model_changed"或"incomplete_turn",model,context_messages}`；仅告知上下文重建，不暴露 Codex thread ID |

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

错误统一 `{error:{code,message}}`；400 参数错误、401 未认证/撤销/无效配对码、403 权限/交易禁用、404 不存在、409 幂等/归档/忙碌/模式冲突、413 超过请求体限制、415 非 JSON、429 配对限流、500 服务错误、503 Codex 账号服务不可用或操作结果未确认。

错误不返回模型原始异常、账户信息、密钥或服务器文件路径。配对按来源 IP 每分钟最多五次尝试。

已接受但无产品最终回复的运行，重启时先检查 Runtime 是否已保存匹配该输入的完整回复；确定有完整缓存时直接补齐产品结果，不再调用模型。其余标为 `run_interrupted`，不自动重跑模型或工具。当前没有交易工具；未来交易工具必须另行实现授权、幂等和券商对账，不能借聊天重试直接执行。

Codex 使用 app-server JSONL 流的 `item/agentMessage/delta` 即时产生既有 `assistant.delta`，不等待 turn 结束再统一输出；failed/interrupted 不能生成 `assistant.completed`。登录缺失产生 `assistant.failed` 的 `login_required` 错误码；它不代表设备配对失效，不应清空设备凭据。Codex 子进程退出、协议超时和不确定运行不会自动重放。

app conversation 与 Codex thread 的映射只保存在后端 Agent 状态中。正常重启后先 `thread/resume` 再创建新 turn；更换模型或上个 turn 未确定完成时，以明确的新 thread 重建引用上下文。不同 Runtime 模式的旧状态不自动混用，切换 demo/pydantic/codex 时应新建产品会话；原会话记录保留。

本协议已定义设备认证、增量补齐和 HTTPS 接口；公网可达性、可信证书配置、iOS 后台推送及真机测试分别验收，不能仅凭本机 HTTP 测试认定完成。
