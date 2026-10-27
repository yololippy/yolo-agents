# Agent 与产品存储实现说明

本说明对应 `store.py`、`agent_runtime.py` 和它们的测试。通信协议、设备认证及服务调度由服务入口负责。没有券商连接、行情读取或交易执行工具。

## 当前可以运行的部分

- `Store`：标准库 SQLite，产品会话、原消息、增量事件、独立 Agent 状态；可跨线程使用，同一文件的不同 Store 连接通过 SQLite 事务协调。
- `DemoRuntime`：无需第三方依赖，所有回复显式带“演示模式／未连接真实模型或 IBKR”，用于验证聊天、持久化和设备同步。
- `PydanticRuntime`：真实模式适配代码，采用研究文档中的官方 Core/Harness 组合。缺依赖、版本不匹配、密钥/模型配置错误或模型运行失败都会报错，不回退到演示模式。
- `CodexRuntime`：通过本机 Codex app-server 复用受管 ChatGPT 登录，不需要 API key。仅聊天，关闭工具入口；即时映射 agentMessage 增量，并持久保存会话模型和 Codex thread 映射。使用与验收边界见 [Codex 接入说明](CODEX.md)。

**验证状态（2026-10-27）：** main 已将固定版本代码依赖安装到根 `.venv`。已在 Python 3.14.9 上执行标准库存储、演示和适配边界测试，并用真实官方 SDK 的 TestModel／FunctionModel 验证同步流、Conversation 序列化与重建 Runtime 后续聊、官方 SQLite Memory 写读、StepStore 快照、跨会话历史搜索及摘要压缩后原文召回；这部分测试将 `ALLOW_MODEL_REQUESTS=False`，不使用真实 API key。Codex 路径另已使用受管 ChatGPT 登录实测模型目录、流式推理、模型切换和服务重启续聊，全程不读取 token。contract double 测试仍用于故障注入；PydanticAI 供应商行为和中文语义召回质量尚未验证。

## 存储接口与自动事件

`Store(path)` 提供会话 create/list/get/update、消息 append/list、事件 append/list、Agent 状态 get/save，及 `finish_run`。返回值是 JSON 可序列化字典/列表，时间采用带 `Z` 的 UTC ISO8601。缺失会话的读取 `get_conversation` 返回 `None`；依赖已存在会话的其他操作抛 `KeyError`。

| 操作 | 同一事务产生的事件 |
| --- | --- |
| `create_conversation` | `conversation.updated`，payload 为 `{conversation: ...}` |
| `update_conversation` | 有实际修改时产生 `conversation.updated`；无变化不产生事件 |
| `append_message` | `message.created`，payload 为 `{message: ...}`，然后 `conversation.updated` |
| `finish_run(..., status='completed')` | 最终助手消息、`message.created`、`conversation.updated`、`assistant.completed`；最后一个 payload 为 `{run_id, message}` |
| `finish_run(..., status='failed', error=...)` | 同上，但最终事件为 `assistant.failed`，payload 为 `{run_id, message, error}` |
| `save_agent_state` | 不产生产品事件，不把模型内部状态发送给客户端 |

`append_message` 的 `client_message_id` 在会话内唯一；重复 ID 且 role/content 相同返回原消息，不再写事件；不同内容或角色抛 `ValueError`。不同会话可使用相同 ID。没有幂等 ID 的消息每次追加。

`finish_run` 只接受 completed/failed。一个 `(conversation_id, run_id)` 只能有一条最终助手消息，先提交的最终结果胜出，后续调用返回原结果且不重复事件；同 run 的用户消息不受该唯一约束影响。请通过 `finish_run` 完成运行，避免自行分开写助手行和最终事件。

事件 `seq` 是数据库级递增游标。`list_events(after=N)` 使用严格大于，按 seq 升序分页；带 conversation_id 时过滤同一全局事件序列。事件不自动过期，产品记录不因模型上下文摘要化而删除。

`list_conversations(query=...)` 对标题与保留的产品消息做 Unicode casefold 后的字面子串查询，中文可匹配子串，`%`/`_` 不作为通配符。这是产品列表搜索，与官方 Agent BM25 召回不同，不应称为语义搜索。

归档不删除消息，默认列表隐藏归档会话。是否允许向归档会话发消息由服务层校验；底层存储允许维护其历史数据。

## Runtime 接口与集成要求

```python
runtime = create_runtime(mode, model, data_dir, store)
# mode/model_name 用于健康状态；不包含模型密钥。
for delta in runtime.stream_reply(history, conversation_id, user_id="local-user"):
    ...
```

- mode 必须显式为 `demo`、`pydantic` 或 `codex`。Codex JSONL 适配使用标准库，不额外安装 Python 模型 SDK；机器上需要可用的 Codex CLI。
- `history` 只能来自服务端已持久化的消息，末条必须是本次用户消息。不可接收客户端伪造的工具、批准或其他会话历史。
- `user_id` 是认证后的账户身份，不是设备 ID。当前多设备连接同一位本地用户，使用相同 `local-user`；若以后增加多用户，API 仍须负责会话所有权。
- 一个本地服务共享一个 Runtime。Runtime 对同会话运行加锁；服务层仍负责消息接收顺序、排队和跨进程防重入，不能把 Python 锁当成多进程租约。
- 同步流需要在同一个 worker 线程中创建、迭代并关闭，不要把迭代器分派到多个线程。真实 Agent 按用户和 worker 缓存，避免跨线程共用同一同步流的 provider 客户端。
- 服务退出时，先停止接收消息并等待服务层 worker 结束，再调用 `runtime.close()`，最后关闭产品 Store。Runtime 会清理自己创建的事件循环；调用者原有的事件循环保持由调用者管理。存在尚未关闭的运行迭代器时，`close()` 会明确拒绝，不能在仍生成回复时释放资源。
- Runtime 不写最终产品助手消息，由服务层调用 `finish_run`。流式异常必须结束为失败；不得拼接一段演示文字假装真实生成成功。

真实模式直接使用官方 `Agent`、`Memory`、`SqliteMemoryStore`、`StepPersistence`、`SqliteStepStore`、`ConversationSearch`、`SnapshotHistorySource`、`SummarizingCompaction`。没有另写记忆框架，也没有注册账户或下单工具。

每位用户的官方数据库位于：

```text
<data_dir>/users/user-<sha256(user_id)>/agent.sqlite3
<data_dir>/users/user-<sha256(user_id)>/memory.sqlite3
```

`scope='all'` 只搜索这位用户的物理 store。namespace 同样从服务端身份生成，避免名称包含路径内容。跨会话共享同一用户的记忆；同用户不同 worker 的官方 store 指向同一 SQLite 文件。

已用真实 SDK 复现并修复首次并发初始化竞争：多个新会话同时打开全新官方 SQLite 文件时，首次 `journal_mode=WAL` 切换可能被另一个连接阻塞。Runtime 在初始化保护锁内先为两个文件配置 WAL，再交给官方存储管理全部表结构和数据；后续模型运行不被该锁串行化。真实 SDK 的四个 worker 同时新建会话已覆盖这一场景。

应用 Agent 状态采用版本化 JSON envelope，内含官方 `ConversationTypeAdapter` 序列化的 conversation，以及 user_id/mode/最后一个输入 ID/最后一条完整回复。继续运行只反序列化服务端 conversation，不从产品文本猜测 usage、工具结果或批准。

官方同步流公开 messages、usage 和 conversation_id；本实现使用这些字段构造官方 `Conversation` 后经官方 adapter 保存。当前严格使用文本输出且没有 deferred tools，因此待批准项为 None。如果加载到已有待批准项，明确拒绝自动恢复，不把它丢弃或自动批准。后续加入批准工具时，需要改用支持完整 deferred result 的路径并补恢复测试。

已接受用户消息中的服务端 `run_id` 会传给官方运行，便于关联快照和最终消息。累计 usage 被保留；每次运行限制在既有累计值之上增加最多 8 次模型循环、24 次工具调用和 60,000 token，最终输出最多 2,048 token，运行超时 90 秒。token 限制通常在响应后校验，不是绝对费用保证；SDK 的网络重试也不等于 Agent 验证重试。`retries=0` 禁止应用模型验证/工具重试，但不声称已控制所有供应商网络重试。

## 失败、取消和恢复

模型流失败或消费者提前关闭时，不用部分输出覆盖最后成功的 Conversation，不自动重试工具。官方步骤存储仍可能记录失败轨迹或已完成记忆写入，业务恢复不能仅看最后成功的会话 JSON。

固定 SDK 版本直接接收到同步生成器的 `GeneratorExit` 时，会在退出异步运行上下文时产生 `coroutine ignored GeneratorExit`。本适配先显式关闭内部流式迭代器，并以不接受输出的正常路径退出 SDK 上下文，避免把 `GeneratorExit` 传入该生命周期。真实 FunctionModel 测试确认生成器被及时关闭、没有保存部分会话，且随后新会话可以继续运行。

成功结果先保存官方状态 envelope，随后由服务层写最终产品消息。这两个动作不是一个跨存储事务。envelope 缓存当前输入 ID、完整回复和完成标识；若同一已成功输入在最终消息提交前被重试，Runtime 返回已缓存结果，不再次调用模型。

两种 Runtime 均提供 `recover_completed_reply(conversation_id, user_message_id, user_id='local-user') -> str | None`。启动恢复先调用此只读方法，输入 ID 匹配且状态包含完成文本才返回，再由服务层 `finish_run` 补齐最终产品消息和事件；没有匹配完成结果时返回 None，仍由服务层标记中断，不自动重放模型/工具。状态损坏、模式不符或跨用户访问抛 `RuntimeStateError`；真实模式还验证官方 Conversation 的格式、会话 ID 及无待批准项。恢复不会创建 Agent、打开官方记忆/快照 store 或调用模型/工具，也不会写数据库。旧版本 1 envelope 虽没有完成标识，同样只在成功后写入，继续兼容读取。

若没有成功官方状态却已有多条产品历史，真实模式要求新建会话，避免把客户端或失败运行的文本误当成可恢复的官方工具历史。演示/真实状态也不混用；切换模式请新建会话。

记忆自动注入失败采用 `injection_errors='raise'`，便于首轮验证明确暴露存储故障。后续可以设计聊天降级，但交易权限、限额、确认仍不能依赖记忆。

官方 ConversationSearch 目前是 BM25，连续中文存在已记录的分词限制。产品消息子串搜索成功，不代表官方 Agent 历史召回质量已通过验证。

## 依赖与后续真实验证

标准库路径可以直接测试，无需安装：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python python3 -m unittest discover -s packages/core-python/tests -p 'test_store.py' -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python python3 -m unittest discover -s packages/core-python/tests -p 'test_agent_runtime.py' -v
```

已安装的官方 SDK 离线测试使用根虚拟环境运行；没有该固定版本依赖的解释器会明确 skip 这些用例，不会把它们算作通过：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python .venv/bin/python -W error::ResourceWarning -m unittest discover -s packages/core-python/tests -p 'test_agent_runtime.py' -v
```

真实模式所需版本写在 `packages/core-python/requirements.txt`：

- `pydantic-ai-slim[openai,anthropic]==2.61.0`
- `pydantic-ai-harness==0.61.0`

代码依赖已获用户授权，由 main 统一安装到根 `.venv`，避免多个会话并发修改环境；完整软件安装仍需先告知用户。本实现不会自行运行安装。供应商的模型名和环境变量密钥由用户配置，代码不打印它们，也不将原始供应商异常发给客户端。其他供应商需要选择相应 extra 后再验证。

官方 TestModel／FunctionModel 离线验证已补入可选集成测试，包含真实 Harness 工具执行与流式失败，所有数据均位于测试临时目录。后续由用户配置真实模型，验证供应商流式行为、费用、中文召回、实际记忆写入决策及长时间运行；确定性离线模型不证明真实模型会正确选择工具或记忆内容。

参考：[官方同步流源码（v2.61.0）](https://github.com/pydantic/pydantic-ai/blob/v2.61.0/pydantic_ai_slim/pydantic_ai/result.py)、[完整 Conversation](https://github.com/pydantic/pydantic-ai/blob/v2.61.0/pydantic_ai_slim/pydantic_ai/conversation.py)、[官方 Harness](https://pydantic.dev/docs/ai/harness/)、[项目调研](../../docs/RESEARCH_PYDANTICAI.md)。
