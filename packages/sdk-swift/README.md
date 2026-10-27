# SDK Swift

Swift 客户端通信 SDK：设备配对、认证、会话、消息、事件同步与本地状态。它不依赖 SwiftUI，可被 macOS、iOS 或其他 Swift 客户端复用。

`YoloAgentsProbe` 是使用该 SDK 的本地联调可执行程序。

## Codex 账号

`account()`、`startAccountLogin()`、`logoutAccount()` 使用 `/v1/codex/account`、`/v1/codex/account/login`、`/v1/codex/account/logout`，携带设备 Bearer 凭据；POST 仅发送空 JSON 对象。账号状态支持后端 `{account:{status,email,plan_type}}` 包装，登录响应为 `{login_id,auth_url}`。

私有账号接口仅供具有 `devices:manage` 的 macOS 设备使用。iOS 的脱敏状态来自 `models().auth`。`CodexLoginResponse.authorizationURL()` 与后端一致，只接受 `https://chatgpt.com` / `https://auth.openai.com` 的授权地址，拒绝其他 host、用户凭据、非标准端口、fragment、控制字符及令牌查询参数。SDK 不读取或缓存 Codex 受管 token。

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
