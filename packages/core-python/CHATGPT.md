# 本 Agent 的 ChatGPT 授权模式

`--mode chatgpt` 使用 PydanticAI 的 `OpenAIResponsesModel`、`OpenAIChatGPTProvider` 和官方 Harness。每个 Agent 使用独立数据目录、OAuth 注册、凭据、会话和记忆；该 Agent 的 Mac 与 iOS 设备访问同一后端。`codex` 模式保留，两个模式不读取或混用彼此的认证和运行状态。

需要 `pydantic-ai-slim[openai]==2.61.0`、`pydantic-ai-harness[skills]==0.61.0`。官方 Provider 使用公开 `https://api.openai.com/v1/models`（`models`／`slug`／`visibility=list`）获取账户目录；推理使用 `/v1/responses`，SDK 强制 `stream=true`、`store=false`。不依赖 Codex app-server 或 `~/.codex` 中的凭据。目录只表示目录项，实际 entitlement、额度和请求成功仍需实际响应确认。

首次登录生成并持久化此实例的 `urn:uuid:` host ID；官方 OAuth flow 使用新 state、nonce、PKCE，并先在 `127.0.0.1` 的随机端口监听 `/auth/callback`，向 Mac 返回授权 URL。用户必须手动在浏览器同意。成功后保存官方返回的 client ID、access/refresh/ID token 与 scope；后续登录复用注册 ID，不把 ID token 放入登录 URL。后台不会读取或传出任何 token。未授予 `chatgpt.tokens.use.direct` 时显示 `permission_required`，不能推理。

`chatgpt-auth/` 权限 0700，凭据和注册文件 0600，写入先 fsync 临时文件再原子替换。各线程和进程使用文件锁串行访问和刷新同一授权；Host 还持有数据目录单实例锁。刷新失败或保存失败不回退旧 token；保存未确认时显示 `credential_persistence_failed` 并要求重新登录。目录请求超时 10 秒，推理读取超时 30 秒且总运行限时 90 秒；OpenAI SDK 通用重试关闭。官方 OAuth Provider 对确定的 HTTP 401 可刷新并重发一次；未知中断的产品消息不自动重放，`chatgpt-runs.sqlite3` 记录已启动输入，即使进程退出也不会重复发起该输入。

官方工具的可恢复错误（例如读取尚不存在的记忆文件）最多允许两次纠错，让模型收到工具反馈并继续回答；这发生在同一次受限 Agent 运行内，不会自动重放失败或结果不明的外部模型请求。

单次使用的 refresh token 发出前先持久化不确定标记，完整新凭据成功落盘后才清除。刷新超时、进程中断或写入失败时该标记跨重启保留，禁止再次消费旧 token；远程撤销未确认的标记也跨重启保留。

退出尝试发现并调用官方 revocation endpoint，然后清除本地 token；未确认远程撤销时返回 `remote_revocation_unconfirmed`，用户可在 ChatGPT 设置断开应用。取消或退出使迟到的 callback 无法激活账号；若迟到的换码已产生凭据，则尝试撤销并在未确认时报告。账号变更与已接受/排队的聊天使用同一准入锁，目录获取及实际运行也参加账号操作保护。

各 Agent 默认 `~/Library/Application Support/YoloAgents/<agent-id>/`，目录标记记录归属并拒绝跨 Agent 或父子目录重用。未标记的非空自定义目录需要 `--adopt-existing-data` 明确接管；检测到旧 Trading 根目录存在聊天时，新的 Trading 默认路径不会静默开启空库。显式使用旧根目录需 `--data-dir <旧目录> --adopt-existing-data`；B Agent 不能接管旧 Trading 根目录。Host 和 Trading 入口在创建账本或连接券商前调用 `prepare_agent_directory`，Host 再持锁核验一次。

若过渡期将旧根目录本身接管为 Trading，默认 B Agent 目录会成为 Trading 的子目录并被隔离检查拒绝；此时 B 必须显式使用旧根之外的目录。建议完成受控迁移后，两个 Agent 使用同一未拥有父目录下的独立兄弟目录；本实现不会自动移动生产数据。

本实例 `skills/` 只读取目录内普通 `SKILL.md`，限制数量和大小，拒绝跟随符号链接和硬链接；内容复制到临时私有快照后交给官方 `Skills`。不加载全局 Codex Skills 或其他 Agent 的文件，也不提供对技能目录附带脚本的执行能力。Memory、ConversationSearch、StepPersistence 和 Compaction 使用官方 Harness；历史和记忆按服务端用户及 ChatGPT 注册主体隔离。历史搜索是词法检索，中文召回不保证完整。

注册主体使用经验证的 ID token `sub` 与该授权签发的 `client_id` 组合，不假定 `sub` 本身区分 workspace。首次登录由官方 SDK 核验；刷新若返回新的 ID token，本适配另外核验 issuer、audience、有效期和原 subject 一致。依据 [OpenAI SIWC 概览](https://developers.openai.com/siwc/token-sharing-open-source) 的“A client vs. an agent host”，动态注册的 client ID 绑定认证用户及注册时选中的 workspace；隔离因此依赖这一服务端绑定。官方 token 字段说明没有提供本方案可独立验证的 workspace ID；如果上游允许同一个 client ID 在不同 workspace 下被复用，本地不能仅凭 `sub` 区分这两者。

同一 Provider 会话选择新模型时，下一轮创建新的官方 Conversation，以最多 64000 字符的已完成产品文本作为引用上下文；不把旧模型的工具调用或内部状态交给新模型。原产品消息和旧快照保留。Codex/Pydantic/demo 的旧会话可读取产品历史，切换模式需新建会话，不能伪装恢复不同 Provider 的内部状态。

账号接口、错误和字段见 [通信协议](../protocol/COMMUNICATION_V1.md)。离线测试使用模拟 OAuth 和 HTTP 传输。2026-10-28 已在独立配置中完成两实例真实授权、模型流式、模型切换、记忆/Skills 隔离与退出/重启验证；真实令牌到期刷新仍未实测。证据见 [项目验收记录](../../docs/TODO.md)。参考 [OpenAI SIWC](https://developers.openai.com/siwc/token-sharing-open-source)、[PydanticAI ChatGPT Provider](https://pydantic.dev/docs/ai/models/openai-chatgpt/)。
