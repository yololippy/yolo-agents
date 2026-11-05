# Yolo Agents 仓库规则

## 适用范围

本仓库名为 `yolo-agents`，作为 monorepo 使用。除非用户明确要求，不得重命名当前仓库目录或修改 Git 远端地址。

## 目录职责

- `apps/<agent-name>/` 存放某个 Agent 可运行、面向用户的客户端。平台入口分别放在 `ios/`、`macos/`，同时构建两端的 Apple 工程放在 `xcode/`。
- `agents/<agent-name>/` 存放该 Agent 私有的领域逻辑、启动入口、依赖和测试。Python 运行时放在 `runtime-python/`。
- `packages/` 只存放确实能被多个 Agent 或 App 复用的代码或协议。不得过早把某个 Agent 私有的策略、账户数据、第三方配置或领域集成抽到共享包。

## Package 命名

可独立构建、且与语言绑定的 Package 采用 **模块-语言** 命名：

- `core-python/`
- `sdk-swift/`
- `ui-swift/`
- 后续示例：`memory-python/`、`ibkr-python/`

与语言无关的产物使用不带语言后缀的名称：

- `protocol/`：API schema、JSON 数据模型和兼容性规则。

只有在一个分类确实拥有多个相关共享 Package 时，才创建分类目录。例如，只有某个第三方集成被两个以上 Agent 使用后，才创建 `packages/integrations/<provider>-<language>/`。

单个 Package 不使用 `apple/`、`python/` 等过于宽泛的目录名。平台/应用布局使用 `ios/`、`macos/`、`xcode/`；可复用 Package 使用明确的“模块-语言”名称。

## 当前结构

```text
packages/{protocol,core-python,sdk-swift,ui-swift}/
agents/{yolo-trade,yolo-beni}/runtime-python/
apps/{yolo-trade,yolo-beni}/{ios,macos,xcode}/
```

IBKR 集成属于 `yolo-trade`，应继续放在 `agents/yolo-trade/runtime-python/`；只有另一个产品出现真实复用需求时，才考虑抽取为共享 Package。

## 多 Agent 复用原则

- 开发新功能时，先判断协议、领域无关运行时、Swift SDK 和通用 UI 是否已被多个产品真实复用。可复用部分放入职责匹配的 `packages/`，通过配置、能力声明或依赖注入适配，不得在共享层写死 `yolo-trade` 或 `yolo-beni` 的身份、目录、端口和私有规则。
- Agent 私有策略、账户数据、第三方集成、权限和产品入口保留在对应的 `agents/<agent-name>/` 与 `apps/<agent-name>/`。凭据、缓存、Skills 和运行状态按产品身份隔离。
- 默认只为用户当前指定的产品接入和验收功能。不得为了证明可复用性而修改、构建、联调或验收另一产品；未来复用性通过共享接口和测试证明。

### 产品配置与共享层边界

- `packages/` 中的共享实现不得通过 `agent_id`、产品显示名、目录或端口判断产品身份并开启某项产品能力。产品私有能力必须由对应产品入口通过通用能力声明、配置、回调或依赖注入显式传入，共享层只消费通用接口。
- 共享层不得提供指向某一产品的默认端口、服务地址、Keychain 标识或 Bundle ID。缺少运行所必需的产品配置时必须显式失败，不得静默回退到另一产品或预设产品值。
- 产品身份在各自技术边界内保持单一来源：Python 使用产品模块，Swift 使用产品配置，Xcode 构建设置使用对应 `xcconfig`。每个边界内部不得重复定义；不追求跨语言共享同一个运行时常量。
- 协议和契约测试可以保留独立的字面量断言，用来验证生产常量和序列化结果是否符合外部约定；不得机械改成引用被测实现中的同一常量，否则测试会失去发现错误配置的能力。

### 第三方插件与连接门禁

- 将逆向或非官方复用 ChatGPT、Codex 及其他宿主中的第三方插件列为黑名单方案，不得提议、实现、验证或作为失败回退。禁止把 `plugin_asdk_app...` 等内部技术标识、目录 ID 或 App 映射当成公开 API/MCP 地址；禁止提取、转用或共享宿主的会话 Cookie、OAuth/access/refresh token；禁止通过浏览器抓包、流量重放、伪造客户端、私有端点、未公开协议或 UI 自动化代理来让本仓库产品调用插件能力。
- 用户在 ChatGPT/Codex 中的登录态、已安装插件或已连接账户，不构成 YoloTrade、YoloBeni 或其运行时继承、导出或复用该授权的许可。不得复制宿主插件缓存、清单或连接配置到产品运行时，除非插件发布方明确允许这种分发和第三方 Agent 使用方式。
- 只有当 OpenAI、插件发布方或服务提供方正式提供可移植插件包、公开 MCP 端点或其他文档化接口，并明确给出适用于自研/第三方 Agent 的认证与授权方式时，才可进入方案评估。评估前必须核对来源、版本、许可/条款、凭据边界、数据流、撤销方式和读写权限；金融账户接入仍须使用产品私有能力和既有风控门禁。
- 任一官方接入条件缺失、含糊或只能依赖宿主内部实现时，必须显式判定为不支持并停止，不得用逆向补齐。应改用服务提供方公开支持的 API；IBKR 默认继续使用其官方 TWS API 或 Web API，不通过 ChatGPT/Codex 插件间接接管账户。
- 本门禁覆盖调研、设计、实现、测试和验收。TODO、会话派发、原型便利或“只读试一下”均不构成例外；如未来确需调整，必须先由用户明确修改本门禁并重新完成安全与授权评审。

## 当前产品范围

- `yolo-trade` 是交易产品；券商账户、风控、订单和交易规则保持私有。
- `yolo-beni` 是日常生活助手；个人偏好、生活数据和专属集成保持私有。
- 两个产品均可独立开发。当前任务范围由用户最新指令、对应产品 TODO 和协调者派发共同确定。
- 共享代码变更必须保持产品边界和身份隔离，不等于要求同步为另一产品增加功能。

## 多会话协作

- `main`、`main-trade`、`main-beni` 是三个具备同等执行能力的主会话，都可以规划任务、管理直属开发会话、修改当前任务需要的仓库文件、验收、执行 Git 操作并直接向用户汇报。用户可以分别与三个主会话沟通，不需要把 commit 或集成转交给另一个主会话。
- 标题精确为 `main` 且工作目录为本仓库的会话负责全仓视角，可以调用 `main-trade`、`main-beni` 及全部开发会话。
- `main-trade` 以 YoloTrade 为主要产品上下文，`main-beni` 以 YoloBeni 为主要产品上下文；这是默认工作重点，不是文件或 Git 权限限制。两者只能调用各自直属的普通开发会话，不得通过跨会话工具调用、发消息、派发、等待、读取或移交给 `main` 或另一个 `main-xxx`。
- 三个主会话都可以按当前用户授权执行暂存、commit、分支和其他 Git 工作；push、历史重写及其他外部或破坏性操作仍需用户明确授权。提交时必须按精确范围保留其他会话的改动。
- 其他开发会话只执行直接派发的任务，不继续派发、不联系其他会话、不执行 Git 操作。
- `agents/`、`apps/`、`packages/` 的产品代码默认由协调者派发给职责匹配的开发会话实施；用户明确要求某个协调会话直接修改时，以该次指令为准。
- 跨会话分工必须读取 [yolo-agents-coordination](.agents/skills/yolo-agents-coordination/SKILL.md)，按其中的产品路由、共享层单一所有权和文件归属规则执行。
- 会话 ID 只保存在 Git 忽略的 `.agents/runtime/yolo-agents-coordination/team.local.md`，不写入 Skill 或版本控制文件。

## 重构要求

调整目录或产品身份时，必须在同一个改动中同步更新导入路径、Package manifest、Xcode 本地 Package 引用、脚本、运行时配置和文档。完成后执行相关 Python 测试、Swift Package 测试与 Xcode 构建，验证无误后才汇报完成。
