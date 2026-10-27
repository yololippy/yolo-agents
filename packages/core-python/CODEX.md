# Codex 受管 ChatGPT 登录与聊天模式

2026-10-27。协议基线：本机 `codex-cli 0.164.0-alpha.11.1` 生成的 app-server JSON Schema。实现位于 `yolo_agents/codex_runtime.py`；适配是通用聊天能力，没有 IBKR 或交易工具。

## 启动与认证

```bash
PYTHONPATH=packages/core-python .venv/bin/python -m yolo_agents --mode codex
```

交易 Agent 的既有启动命令也可使用 `--mode codex`。`--model`／`YOLO_AGENTS_MODEL` 可指定目录中的默认模型，省略则使用 app-server model/list 标记的默认值。Codex 可执行路径可用 `YOLO_AGENTS_CODEX_BIN` 指定；只接受一个可执行文件路径，不通过 shell 拼接命令。

Codex 自己管理现有登录、凭据存储和刷新。本适配不打开、复制或解析 `~/.codex` 中的 token，不提供 token 上传/下载接口，也不把宿主环境中的 API key、IBKR 凭据传给子进程。CODEX_HOME 若由环境显式指定，只作为 Codex 自己的存储位置传入；应用不直接读取其中凭据。

`account/read` 只有返回 ChatGPT 账户时才允许开始推理；API key 登录不视为满足本模式。缺少受管登录时返回明确 login_required。已配对 Mac 管理设备可启动官方浏览器登录，客户端打开返回的 auth_url；Codex 托管回调、存储和刷新。不调用 external token 模式，不静默切回演示。产品设备 token 与 OpenAI 登录是两套独立凭据。

账号管理接口为 `GET /v1/codex/account`、`POST /v1/codex/account/login` 和 `POST /v1/codex/account/logout`，两个 POST 只接受 `{}`。三个接口均要求配对记录为 macos 且具有 devices:manage；iOS 和聊天权限设备返回 403。状态仅提供 status/email/plan_type，启动登录仅提供经官方 HTTPS 主机与格式校验的 login_id/auth_url，绝不转发原始账户或登录响应。模型目录依然不提供邮箱。详见 [通信协议](../protocol/COMMUNICATION_V1.md#codex-账号管理)。

登录固定传 `type:chatgpt`、`useHostedLoginSuccessPage:true`、`appBrand:chatgpt`。客户端轮询账号状态，login_pending 到收到完成通知或该子进程丢失时结束；完成通知里的错误原文不保存或公开。登录等待期间不启动新推理。任意会话有已接受、排队或正在生成的回复时，登录和退出均返回 409 codex_busy；消息接受与账号变更共用准入锁，Runtime 另有独立保护。此锁只覆盖本服务，无法阻止外部 Codex 客户端更改同一受管登录。503 或连接失败时不自动重发账号变更，应先查询实际状态。

官方受管登录与 app-server 生命周期见 [Codex app-server](https://developers.openai.com/codex/app-server/)。[Sign in with ChatGPT app-server 页面](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) 描述的是应用自己持有 OAuth token 的另一条接入路径；本实现没有照搬该示例的 ACCESS_TOKEN 环境注入，而是复用 Codex 原生受管登录。

## 模型目录与对话状态

`GET /v1/models` 需要设备认证，返回规范化模型目录、default_model、auth 状态和 `catalog_only:true`。只保留 ID、显示名、描述和默认标记，不返回原始 account/read 或配置内容。目录不是 entitlement 检查：真实推理完成状态才验证本次调用是否获准。详见 [model/list 与 turn 生命周期](https://developers.openai.com/codex/app-server/)。

会话的可空 `model` 存在产品 SQLite 中，schema 从 1 升到 2。旧会话默认 null；PATCH 省略保持原模型，显式 null 恢复默认。运行或排队期间不允许切换模型。

Codex thread ID、选定模型、最后输入 ID、完成文本及 turn 状态保存在独立 Agent 状态 JSON 中，不发给客户端。完成结果可以在“Runtime 已保存、产品最终消息尚未提交”的窗口只读恢复。重新启动子进程后使用 thread/resume，重新应用同样的权限配置；不重放旧 turn。

模型改变时下次请求显式新建 thread，把已完成的产品聊天作为引用文本上下文，最多约 64,000 字符，并记录 agent.thread.rebuilt。上次运行不确定时，只有新的用户输入才可触发新 thread；相同输入不会自动重跑。所有产品聊天记录保留，引用上下文不是工具结果或批准，也不宣称全部历史都已送入新模型。

Codex 模式不替代 Pydantic 模式中的 Harness 长期记忆功能；Codex 本地 memories 功能在本模式关闭，避免让聊天模型访问本机记忆文件。既有 demo/pydantic 状态不自动迁入 Codex，会话记录可继续查看，新模式使用新会话。

## 聊天权限边界

子进程在服务数据目录下的独立空工作目录启动，不使用仓库、用户主目录或券商数据目录作为 cwd。参数通过 argv 传递，shell=False；stderr 不转发或记录，JSONL 输入/输出、通知队列、RPC 和 turn 均有上限/超时。

使用只读 sandbox、禁用权限升级的 approval policy，并通过 CLI 配置关闭 shell、浏览器/电脑操作、图片文件工具、代码执行、子 Agent、apps/plugins、hooks 和其他本机能力。初始化后通过受支持的 config/read 核验有效安全配置，不符合时停止。当前版本不再允许显式设置 untrusted，因此采用 never，配合工具关闭、只读隔离和拒绝全部服务端请求，不代表允许任何提权。

**不能用 `mcp_servers={}` 假定清空继承配置。** TOML 表会合并。本机探测发现这一点后，适配改为：仅初始化并获取配置中的服务名称，关闭首个子进程，再为每个已配置服务器传入 enabled=false/required=false，重新启动并核验全部禁用；此过程发生在创建任何 thread 之前。hooks 也可能继承表内容，通过 hooks feature=false 禁用，而不依赖空表覆盖。

thread/start、thread/resume、turn/start 重新指定受限 sandbox/cwd；不接受客户端传递这些字段。启动 thread 不注册 dynamic tools、环境或平台 capability roots，禁用模型回退，并校验返回的模型、provider 和 sandbox。JSONL RPC 仅允许初始化、配置核验、账户状态/受管浏览器登录/退出、模型目录、thread/start/resume、turn/start/interrupt；没有通用 RPC 代理，模型不能调用账号管理方法。

收到服务端工具/批准/权限/外部 token 刷新请求时，一律返回拒绝并停止子进程；出现 commandExecution、fileChange、MCP 等非聊天 item 也停止，不执行任何回调。模型不能通过产品聊天获得券商入口或认证接口。

这些边界面向已核对 CLI 版本。升级 Codex 时必须重新核验有效配置和工具边界；配置 schema 的字段存在不代表该版本仍接受所有旧枚举。

## 验证状态和 main 验收

已加入完全隔离的 JSONL 子进程测试，不依赖真实联网、真实登录或付费模型。覆盖模型目录分页、环境隔离、有效配置不匹配、继承 MCP 禁用、实时 delta、成功/失败/中断、重启 resume、模型切换、RPC 错误、子进程退出、超时、权限请求拒绝和旧数据库升级。

账号管理增量也只运行隔离测试：真实本机 HTTP/配对/SQLite 搭配模拟 app-server，覆盖 Mac 管理权限、iOS/聊天权限拒绝、请求中撤销、额外凭据字段过滤、恶意登录 URL、登录完成/失败、排队与运行中的退出拦截和静态错误。没有执行真实 account/login/start 或 account/logout；浏览器交互与真实退出由后续人工验收。

main 随后使用隔离的临时服务数据和本机真实 Codex CLI 完成受管登录验收，全程没有读取或复制用户 token：`account/read` 返回 ChatGPT 登录，`model/list` 返回 8 个可选模型；真实推理收到 9 个 `agentMessage/delta` 后完成，拼接文本与最终文本一致。切换到 `gpt-6-luna` 后创建新 thread 并成功回复；重启服务后通过保存的 thread ID 恢复并继续回复。测试目录、临时设备和 token 均已删除。

后端测试：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python .venv/bin/python -W error::ResourceWarning -m unittest discover -s packages/core-python/tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python .venv/bin/python -W error::ResourceWarning -m unittest discover -s agents/trading-agent/runtime-python/tests -v
```

Mac App 也已通过稳定开发签名连接正式本地服务，实测模型菜单、新会话、输入、模型切换和真实回复；一次较长回复收到 120 个 `assistant.delta` 后以 completed 收敛。Codex 子进程的只读隔离和工具关闭同时由配置核验与故障注入测试覆盖。套餐额度、不同模型 entitlement 和网络故障仍不能由目录查询保证；Apple 真机、远程 HTTPS 和弱网验收独立进行。本次后端实现没有修改 Info.plist 或 project.pbxproj；工作区中这两个文件的既有签名改动不属于本批后端代码。
