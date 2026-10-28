# ChatGPT Provider 与各 Agent 独立授权

更新日期：2026-10-28。状态：代码、自动化验证及独立配置的真实浏览器授权、模型调用、记忆/Skills 隔离验收已完成；生产服务迁移与 iPhone 真机仍待单独验收。

## 1. 决策

采用 PydanticAI Core + 官方 Harness + `OpenAIChatGPTProvider` + `OpenAIResponsesModel`。新增明确的 `chatgpt` 运行模式，使用本应用自己的 Sign in with ChatGPT 授权和公开 Responses API。`codex` 模式保留为显式回退，不读取或复制 Codex 登录凭据，不在请求失败时自动切换模式。

`Yolo Agents` 是仓库与共享框架名称，不作为两个业务 Agent 共用的授权身份。Trading Agent 与 B Agent 分别注册、分别登录与退出。B Agent 本轮仅提供独立授权和聊天骨架，没有交易工具。

## 2. 实例与双端关系

| 内容 | Trading Agent | B Agent |
| --- | --- | --- |
| 固定实例 ID | `trading-agent` | `b-agent` |
| 用户看到的授权名称 | `Trading Agent` | `B Agent` |
| 默认回环服务端口 | `8765` | `8766` |
| 默认 Mac 数据目录 | `~/Library/Application Support/YoloAgents/trading-agent/` | `~/Library/Application Support/YoloAgents/b-agent/` |
| OAuth 注册、令牌、退出 | 本实例独立 | 本实例独立 |
| 产品聊天、Harness 记忆与快照 | 本实例独立 | 本实例独立 |
| Skills 与工具配置 | 本实例独立 | 本实例独立，不导入 IBKR |
| Mac/iOS 本地配对与缓存 | Trading App 专属命名空间 | B App 专属命名空间 |

同一 Agent 的 Mac 和 iOS 客户端连接同一个后端，因而共用会话和记忆。OAuth 登录在 Mac 上完成，由后端保存令牌；iOS 只持有自己的设备配对凭据，不接收 OAuth 令牌。界面应显示当前 Agent 身份，防止将 B App 配到交易服务。

独立授权不等于独立套餐额度。同一个 ChatGPT 账号的套餐限制仍由 OpenAI 决定；不得利用多实例绕过限制。登录也不会导入 ChatGPT 网站的聊天或记忆。

## 3. 代码职责

- `packages/core-python/`：通用 OAuth/Provider、生命周期、实例归属校验、会话存储、Harness 能力与流式事件适配。
- `agents/trading-agent/runtime-python/`：Trading Agent 身份和启动入口、交易工具边界、IBKR 私有集成。模型登录迁移不改变订单确认与执行权限。
- `agents/b-agent/runtime-python/`：B Agent 身份和启动入口；不依赖 Trading Agent。
- `packages/sdk-swift/`、`packages/ui-swift/`：通用账号协议、浏览器登录与状态界面、实例参数、按命名空间保存配对和缓存。
- `apps/<agent-name>/`：本 App 的身份、Bundle ID、缓存/Keychain 命名空间、平台入口和私有界面。Paper 代码仍在 Trading Agent 的 `client-swift/`。

共享的是实现代码。授权注册、运行时实例、数据库、Skills 和业务权限不共享。

## 4. 授权与凭据生命周期

1. 为当前 Agent 的本机实例生成并持久化不含个人信息的 host ID。应用名称固定，后续登录复用已签发的注册信息。
2. Mac 管理设备发起登录；后端只在回环地址监听 OAuth callback，使用官方 SDK 的 state、PKCE 和身份验证机制。
3. 浏览器由用户完成登录与套餐使用同意。用户拒绝套餐授权时明确显示，不能把“取得身份令牌”当作可以推理。
4. 凭据保存在实例受保护的本地存储，目录权限 `0700`、文件权限 `0600`，原子替换；令牌刷新串行，保存最新轮换结果。
5. 账号状态返回脱敏的状态与必要身份，不返回 access/refresh/ID token，也不记录带身份提示的授权 URL。
6. 取消登录使待完成的授权失效；迟到 callback 不得重新激活已取消或已退出的账号。
7. 退出先阻止新推理，再按官方会话撤销方式处理本应用授权。若仅完成本地退出而远程撤销未确认，界面明确说明，不影响另一 Agent 或 Codex 的受管登录。

不同实例不能打开同一个数据目录；归属标记与 Host 单实例锁共同约束。相同目录中的多个并发进程不能竞争刷新令牌。

## 5. 模型与流式回复

模型目录通过本应用令牌查询公开 `https://api.openai.com/v1/models`，只展示实际可用模型，不复用 Codex 内置目录。推理使用公开 `/v1/responses`，遵循此授权流程的 `stream=true`、`store=false` 要求，不调用 ChatGPT 私有 `backend-api`。

共享 HTTP/SSE 协议继续使用 `assistant.delta`、完成和失败事件。真正消费供应商增量，不能把完整回复事后切片冒充流式。只有完整成功响应才能保存已完成状态；限额、授权失效、网络断开和失败不能误报完成，不自动重发未知结果。

新会话和已有会话的模型选择都要落实到该次 Provider 调用。同一 Provider 会话更换模型时，创建新的官方 Conversation，最多携带 64000 字符的已完成产品文本作为引用上下文；保留原聊天和快照，不迁移旧模型的内部工具状态。账号变更与请求准入共用锁；正在生成或排队时拒绝危险的账号切换，客户端展示可操作的状态。

## 6. 记忆、Skills 与上下文隔离

产品聊天与 Harness 状态保存在当前实例的数据目录。Harness Memory、ConversationSearch、StepPersistence 和 Compaction 继续使用官方组件，并在实例内按后端认证用户、ChatGPT 主体及工作区隔离。跨会话检索只访问相同范围的历史，切换 ChatGPT 账号不会沿用原账号记忆。实现采用经验证的 `sub + client_id`；工作区隔离依赖官方对注册 ID 与所选工作区的绑定，不假定 `sub` 单独区分工作区，详情见 [运行时说明](../packages/core-python/CHATGPT.md)。

Skills 从当前实例 `skills/` 目录加载；不自动遍历仓库其他 Agent、全局 Codex Skills 或另一实例目录。拒绝通过符号链接越过配置边界。本轮加载范围是各 Skill 的普通 `SKILL.md`，不加载附带资源或执行脚本。Skills 不能赋予未注册的工具权限，不能把文档里的文字当作订单执行授权。

客户端配对、历史缓存和 Keychain 使用 App 专属命名空间。Trading 保留已有本地缓存的兼容路径；B Agent 不读取旧 Trading 缓存。

## 7. HTTP 与界面

新增通用账号接口：

| 接口 | 用途 |
| --- | --- |
| `GET /v1/account` | Mac 管理设备读取本 Agent 的授权状态 |
| `POST /v1/account/login` | 创建浏览器授权流程，返回登录 ID 和受信 URL |
| `POST /v1/account/login/cancel` | 取消当前待完成的授权 |
| `POST /v1/account/logout` | 退出当前 Agent 的账号 |

健康与账号响应增加 `agent_id`、`app_name`，账号响应明确 `method`。既有 Codex 路由保留兼容，不能把 Codex 凭据冒充本应用授权。iOS 仍仅获取模型目录中的脱敏登录状态；管理接口由后端验证有效 Mac 配对和管理权限。

Mac 账号页展示本应用名称、当前授权状态、登录/取消/退出操作，以及远程撤销未确认等真实错误。Provider 模式下退出文案明确只影响本 Agent；Codex 回退模式保留其真实影响说明。

## 8. 迁移与回退

- 不自动删除、移动或覆盖已有生产聊天数据库。旧根目录不能因默认目录变化而被误认为空白新安装；提供明确的旧目录使用或迁移步骤。
- 旧目录首次接管使用 `--data-dir "原目录" --adopt-existing-data`；该选项只允许未标记的旧目录，不能覆盖另一 Agent 的归属。默认 Trading 子目录发现旧根聊天时明确拒绝启动，避免出现“历史消失”的误解。B Agent 不允许接管已知 Trading 旧根。
- Agent 数据目录不允许父子嵌套。若暂时接管旧 `YoloAgents/` 根目录，B Agent 必须用 `--data-dir` 指定根目录以外的位置，例如 `~/Library/Application Support/BAgent/`；不能同时使用默认 `YoloAgents/b-agent/`。正常使用双实例默认路径前，需停服、备份并将旧数据迁入 Trading 专属子目录，再核对聊天、设备和交易配置。本轮不自动搬动生产数据。
- 原 Codex 产品聊天保留可读，Codex thread 与 Harness Conversation 不互相反序列化。迁移后新建 Provider 会话；若支持复制历史，需要明确标记为新会话，不能假装恢复原供应商状态。
- 现有真实 Codex 服务在新路径验收前保留。先用隔离数据目录做 Mac 联调；切换生产服务时核对原参数、数据目录和端口，保留回退命令。
- 不自动登录、撤销或复制另一个 Agent 的身份，不在迁移中触发 IBKR 下单。

## 9. 验收

- 两个后端同时启动，身份、默认端口、数据与归属标记正确；跨实例目录和错误 App 配对被拒绝。
- A/B 的登录状态、凭据、记忆、历史检索与 Skills 互不影响；退出 A 不改变 B。
- 覆盖授权同意/拒绝、取消、过期、轮换刷新、保存失败、远程撤销失败及迟到回调。
- 覆盖账号管理权限、模型目录、逐次模型选择、真实增量转发、失败不误报完成和不自动重试。
- Python 回归、Swift 单测、两个 Agent 的 Mac 与 iOS 模拟器构建通过。
- Mac 上真实浏览器授权、模型请求和记忆召回分别记录结果；需要用户登录/同意时明确提出，不以模拟测试代替真实验收。

真实验收与故障修复证据见 [TODO 的 Provider 验收记录](TODO.md#provider-真实验收证据2026-10-28)。本轮使用独立验收目录和 18765/18766 端口，未迁移或切换生产服务。任务和完成状态统一记录在 TODO，本文件不另建重复进度账本。

## 10. 官方依据

- [OpenAI：授权范围与实例身份](https://developers.openai.com/siwc/token-sharing-open-source)
- [OpenAI：注册与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [OpenAI：账号、令牌刷新与退出](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [OpenAI：模型目录与公开 Responses API](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [PydanticAI：OpenAIChatGPTProvider](https://pydantic.dev/docs/ai/models/openai-chatgpt/)

该套餐授权面向官方支持的开源/本地运行场景；收费或远程托管产品需要按官方要求申请。遵循协议不代表零账号风险，也不代表官方背书本项目的全部实现。依赖基线以仓库 requirements 与实际源码验证为准。
