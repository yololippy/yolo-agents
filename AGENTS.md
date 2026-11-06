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
- 两个产品均可独立开发。当前任务范围由用户最新指令、[ROADMAP](docs/ROADMAP.md) 和协调者派发共同确定。
- 共享代码变更必须保持产品边界和身份隔离，不等于要求同步为另一产品增加功能。

### YoloTrade 持久运行基线

- 当前用户长期选择为：YoloTrade 保持 **Paper 环境、`read_only=false`、`execution_enabled=true`**。后续部署、升级、重启、修复和配置迁移必须保留该能力；除非用户明确要求关闭 Paper 执行或改回只读，不得静默降级、覆盖或要求用户重复确认这一产品级偏好。
- 上述偏好只授权启用 Paper 下单能力，不构成任一具体订单的确认、提交或重发授权。Mac 管理设备可以为已配对 iPhone 单独授予或撤销 YoloTrade 私有的 Paper 交易权限；每笔订单仍须由发起设备明确确认，并经过 Mac Host 的提交前复核、硬风控、账户级串行、幂等和 unknown 不自动重发门禁。Mac Host 是唯一券商执行端；不得据此开放 Live 执行或自动交易。
- 目标 Paper 账户必须由当前官方 Gateway 返回并唯一核验，账户标识只保存在 YoloTrade 私有且权限受限的本机配置中；账户号、用户名和密码不得写入版本控制文件。Gateway 登录凭据继续只保存在产品专用 Keychain 项中。
- 运维改动必须保留当前常驻基线：8765 Host 由 LaunchAgent 管理，Gateway 每日自动重启时间为 Asia/Shanghai 07:30，Supervisor 被动检查本地监听并分别报告本地接口、IBKR 上游和数据就绪；不得恢复为裸 TCP 探测或仅凭 4002 监听宣称券商已连接。
- `1100`／`2110` 恢复继续遵守维护窗口等待、窗口结束后 15 分钟宽限、单次 Gateway 重启预算和多实例失败关闭。任何修改不得引入循环登录、重复 2FA 或订单自动重发。

## 多会话协作

- `main`、`main-trade`、`main-beni` 是三个具备同等执行能力的主会话，都可以规划任务、管理直属开发会话、修改当前任务需要的仓库文件、验收、执行 Git 操作并直接向用户汇报。用户可以分别与三个主会话沟通，不需要把 commit 或集成转交给另一个主会话。
- 本仓库中的“开发会话”专指左侧栏可见、可独立打开和长期保留的会话级 Agent。不得使用当前会话内部的临时子 Agent 代替开发会话，也不得把临时子 Agent 的执行结果登记为开发会话产出。
- 标题精确为 `main` 且工作目录为本仓库的会话负责全仓视角，可以调用 `main-trade`、`main-beni` 及全部开发会话。
- `main-trade` 以 YoloTrade 为主要产品上下文，`main-beni` 以 YoloBeni 为主要产品上下文；这是默认工作重点，不是文件或 Git 权限限制。两者只能调用各自直属的普通开发会话，不得通过跨会话工具调用、发消息、派发、等待、读取或移交给 `main` 或另一个 `main-xxx`。
- 三个主会话都可以按当前用户授权执行暂存、commit、分支和其他 Git 工作；push、历史重写及其他外部或破坏性操作仍需用户明确授权。提交时必须按精确范围保留其他会话的改动。
- 其他开发会话只执行直接派发的任务，不继续派发、不联系其他会话、不执行 Git 操作。
- `agents/`、`apps/`、`packages/` 的产品代码默认由协调者派发给职责匹配的开发会话实施。优先复用合适的活动会话；没有合适的活动会话时创建新的左侧栏独立会话，已归档会话不恢复。用户明确要求某个协调会话直接修改时，以该次指令为准。
- 跨会话分工必须读取 [yolo-agents-coordination](.agents/skills/yolo-agents-coordination/SKILL.md)，按其中的产品路由、共享层单一所有权和文件归属规则执行。
- 会话 ID 只保存在 Git 忽略的 `.agents/runtime/yolo-agents-coordination/team.local.md`，不写入 Skill 或版本控制文件。
- 用户已选择本仓库直属开发与测试会话统一使用 Full Access（`sandbox_mode=danger-full-access`、`approval_policy=never`）。主会话在创建、复用或恢复时必须通过受支持接口实际设置并核对，同时使用准确的工作树目录；仅在 prompt 中声明或修改 Skill 不会自动改变运行权限。文件归属、普通会话禁止 Git 写操作、产品凭据隔离和业务授权边界仍有效。

## 重构要求

调整目录或产品身份时，必须在同一个 PR 中同步更新导入路径、Package manifest、Xcode 本地 Package 引用、脚本、运行时配置和文档。对应 Python、Swift Package 测试与 Xcode 构建在创建 PR 后执行，验证无误后才汇报完成。

## 分支、PR 与后置测试

- 所有代码修改从最新 `origin/master` 创建独立功能分支：`feat/`、`fix/`、`refactor/`、`test/` 或 `docs/`；不得直接修改、提交或 push 到 `master`，不使用 `codex/` 分支前缀。
- 开发 commit 只做范围审查、精确暂存和差异格式检查，不运行测试配对门禁、测试或构建。执行时机以本节为准，通用 commit-workflow 也必须服从本仓库的后置测试策略。用户单独要求某项测试时照常执行。
- 获得本轮 push/PR 授权后，先 commit、push 并创建 PR，再启动测试；不得把创建 PR 作为测试完成后的步骤。功能和测试代码必须进入同一个 PR，不要求每个开发 commit 同时包含二者。
- PR 创建后，由主会话自动按 [PR 流程](docs/PR_WORKFLOW.md) 冻结准确的 head SHA 与 master base SHA，准备独立 worktree，派发最多三个左侧栏可见的只读测试会话并行运行 P0、Mac 和 Simulator 组；随后完成两端真实 App UI 对话和双向同步验收。不得用内部临时子 Agent、只构建、SDK 测试或命令行模型探针冒充双端 UI 验收。
- 测试会话只执行与报告，不修改代码、不提交、不 push、不继续派发；允许固定入口为核对版本执行只读 Git 命令。测试失败由开发会话修复并创建新 commit，推送后重新冻结版本并重跑；旧 head 或旧 base 的报告不得复用。
- 同产品的真实验收使用跨 worktree 的运行锁。候选 App、Keychain service、缓存和对话数据按测试运行隔离；模型执行层禁用业务工具、Skills 和记忆；不替换正式 App、不重启正式 Host、不操作真实 iPhone。
- 合并前重新读取 GitHub PR 与 master 的实际状态，要求所有必需组和双端 UI 验收均为 `passed`，head/base 未变化、工作树未污染、PR 无冲突且无未满足的 GitHub 门禁。已有本轮合并授权时，由主会话使用 `rebase and merge` 合入，保留每个 commit，不 squash、不创建 merge commit；没有合并授权时只报告就绪。
- 任一缺失报告、`failed`、`blocked`、`not_run`、超时、跳过或 dry run 都不能放行合并。PR 后置测试与用户单独触发的完整安装/升级/真机测试是两个入口，不能互相冒充。

## 功能与测试同改

- 每个新功能、可观察行为变化和缺陷修复必须在同一个 PR 中新增或修改对应的自动化测试代码；只运行既有测试、只补文档、只给手工步骤或事后承诺补测均不算完成。测试必须直接覆盖本次行为以及重要的失败关闭、边界或回归场景，不得用无关测试文件、空测试或放宽断言通过门禁。
- 测试跟随代码职责放置：共享 Python、Swift Package、产品 Python 运行时和产品 Swift 客户端分别修改各自测试目录。`apps/<agent>/ios` 与 `macos` 入口只做装配；如需新增可测试行为，先下沉到对应 `client-swift` 并在那里写测试。跨多个职责范围的功能必须为每个受影响范围分别补测试。
- 创建 PR 后运行 `bin/check-feature-tests --base <base-sha> --head <head-sha>`，检查整个 PR 差异中的生产代码与同范围、未删除的测试代码；不检查暂存区，不作为 commit 门禁。配对通过不能替代测试语义审查和具体测试。
- 如果某项行为缺少自动化验证能力，应在本 PR 中补齐基础设施；无法补齐时将 PR 门禁标为 `blocked` 并报告原因，不能自行豁免或以真机手工验收放行合并。

## 安装后的功能验收

- Mac、iPhone 或其他客户端安装、升级或重新配对后，必须验证目标产品的实际功能可用性，不得仅以构建成功、签名通过、App 可启动、图标出现或设备配对成功作为完成。至少核对 Host 身份与健康、客户端在线与同步、产品所需权限和网络路径；包含 AI 的产品还必须确认对应产品自己的 Provider 账号已授权、模型目录可用，并完成一次不会触发业务副作用的真实模型往返。YoloTrade 还必须确认 Broker 为 `connected_paper`、`ready=true`、`data_complete=true`，且不存在 `submitting`、`unknown`、`cancel_pending` 等未决订单。任一必需功能未通过时必须明确报告安装尚未完成、实际原因和唯一必要的用户操作，不得把“已安装”表述为“已可用”。
- 为完成上述验收而需要官方网页登录或授权时，在用户已明确授权 Codex 操作浏览器的前提下，Codex 应自行完成当前产品发起的官方账号选择、同意与回调流程，不得仅打开登录页就要求用户接手。只有密码、双重验证、通行密钥、生物识别或系统级确认等必须由用户本人完成的步骤才暂停并精确说明所需操作。浏览器操作必须限于可见的官方登录流程，不得读取、复制、导出或重放 Cookie、OAuth/access/refresh token 或其他宿主登录凭据。

## 完整测试入口

- 完整测试是用户明确触发的独立后置流程，不是每次功能开发、修复、review 或 commit 的自动步骤。用户明确要求“完整测试”或调用 `$yolo-agents-full-test` 时，读取 [.agents/skills/yolo-agents-full-test/SKILL.md](.agents/skills/yolo-agents-full-test/SKILL.md) 并按目标产品执行。
- 开发 commit 不运行测试；PR 后置测试按上方独立流程执行。不得把完整测试规则解释为每次改动都要安装真实设备。完整测试发现问题时默认完成其余互不依赖的检查并报告，不自动修改产品代码，除非用户另行要求修复。
