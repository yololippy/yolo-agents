# Yolo Agents 仓库规则

## 适用范围

本仓库名为 `yolo-agents`，作为 monorepo 使用。除非用户明确要求，不得重命名当前仓库目录或修改 Git 远端地址。

## 目录职责

- `apps/<agent-name>/` 存放某个 Agent 可运行、面向用户的客户端。平台入口分别放在 `ios/`、`macos/`，同时构建两端的 Apple 工程放在 `xcode/`。
- `agents/<agent-name>/` 存放该 Agent 私有的领域逻辑、启动入口、依赖和测试。Python 运行时放在 `runtime-python/`。
- `packages/` 只存放确实能被多个 Agent 或 App 复用的代码或协议。不得过早把某个 Agent 私有的策略、账户数据、券商配置或领域集成抽到共享包。

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
agents/{trading-agent,b-agent}/runtime-python/
apps/{trading-agent,b-agent}/{ios,macos,xcode}/
```

IBKR 集成目前属于 `trading-agent`，应继续放在 `agents/trading-agent/runtime-python/`；只有另一个 Agent 出现真实复用需求时，才考虑抽取为共享 Package。

## 重构要求

调整目录时，必须在同一个改动中同步更新导入路径、Package manifest、Xcode 本地 Package 引用、脚本和文档。完成后再执行相关 Python 测试、Swift Package 测试/构建及 Xcode 构建，验证无误后才汇报完成。
