# SDK Swift

Swift 客户端通信 SDK：设备配对、认证、会话、消息、事件同步与本地状态。它不依赖 SwiftUI，可被 macOS、iOS 或其他 Swift 客户端复用。

`YoloAgentsProbe` 是使用该 SDK 的本地联调可执行程序。

## Agent 私有接口扩展

`APIClient.request` 提供单次 v1 JSON 请求，复用设备认证、传输、解码与错误处理；不会自动重试。具体领域模型与接口封装由对应 App 的模块持有。Trading Agent 的 Paper 客户端位于 [client-swift](../../apps/trading-agent/client-swift/README.md)。

## ChatGPT 账号与 App 身份

`account()`、`startAccountLogin()`、`cancelAccountLogin()`、`logoutAccount()` 默认使用 `/v1/account` 及 `/login`、`/login/cancel`、`/logout`，携带设备 Bearer 凭据；所有 POST 只发送空 JSON。账号状态读取 `{account:{status,email?,plan_type?,method,can_manage,app_name,agent_id,error?}}`，登录为 `{login_id,auth_url,status?}`，取消和退出也返回账号包装。`AccountRoute.codexLegacy` 显式选择旧路由；SDK 不自动重放或回退 POST。保留旧 Codex 类型别名以兼容调用方。

`ChatGPTLoginResponse.authorizationURL()` 只允许 HTTPS 的 `chatgpt.com` / `auth.openai.com`，拒绝其他 host、用户凭据、非标准端口、fragment、控制字符和令牌参数。OAuth 凭据留在后端；Mac 只在内存展示邮箱和套餐，iOS 只使用模型目录的脱敏状态，不调用账号管理接口。

私有根视图注入 `AgentAppIdentity`，指定 Agent ID、名称、Keychain service、默认地址及旧服务策略。健康响应兼容可空 `agent_id` / `app_name`；配对前和恢复同步前校验服务身份。`ClientRepository(identity:)` 独立保存配对和缓存：Trading 沿用 `com.tradingagent.apple.v1` 和旧键保留数据；B 使用 `com.yoloagents.b-agent.apple.v1` 与 `agent.b-agent.` 键前缀，绝不回读 Trading。只有 Trading 允许缺少身份的旧服务；明确身份不匹配始终拒绝。

2026-10-28：SDK 47 项通过，1 项真实服务测试默认跳过。身份与存储测试使用内存 fixtures，不访问生产 Keychain。

## 模型与流式事件

- `models()` 读取 `/v1/models`，保留 `default_model`、认证状态及 HTTP 200 响应内的服务错误。模型条目仅用于目录展示，不代表实际调用权限。
- `Conversation.model` 可空，兼容没有该字段的旧缓存。创建会话可传模型；更新时 `.model(id)` 选择模型，`.serverDefault` 明确发送 `model:null`，不传参数则不改模型。
- `eventStream(after:)` 用 Bearer 认证和 `Last-Event-ID` 打开 SSE；`URLSession.bytes(for:)` 按到达的字节消费，不等待响应结束。调用方在结束/切换/退后台时调用 `cancel()`。
- `SSEParser` 支持 UTF-8 跨字节分片、BOM、多行 data、LF/CRLF/CR、心跳与扩展字段；只有完整事件才交付，截断尾帧留待重连补齐。SSE id 与 JSON seq 必须一致。
- `ClientState.applying(event)` 与 REST 分页共用归并和 seq 去重。先保存状态再推进游标，流中断后从该游标排空 REST `has_more`，随后重新接 SSE。SDK 不重放消息提交。
- 缓冲区与行/帧大小有上限；积压时明确报错并让调用方重连补齐，不静默丢失事件。流式网络层与普通请求一样禁用重定向、cookies、持久 HTTP 缓存和 TLS 绕过。

```swift
let stream = try await client.eventStream(after: state.cursor)
defer { stream.cancel() }
for try await event in stream {
    let next = try state.applying(event)
    // Persist next before publishing it or reconnecting from its cursor.
    state = next
}
```

B Agent 健康响应不包含交易专属 `trade_execution_enabled`；共享 Health 解码在缺失或 null 时按 false 处理，类型错误仍拒绝，服务身份等必要字段保持严格校验。新增回归测试覆盖此响应及完整配对流程。
