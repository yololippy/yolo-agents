# Yolo Agents

运行在个人 Mac 上的多产品 Agent monorepo。当前包含：

- **YoloTrade**：对话式 IBKR 助手，交易能力和账户数据保持产品私有。
- **YoloBeni**：日常生活助手，个人数据和生活集成保持产品私有。

## 目录

```text
packages/                       跨产品复用
  protocol/                     通信协议与兼容规则
  core-python/                  Host、认证、存储和通用 Runtime
  sdk-swift/                    Swift 通信 SDK
  ui-swift/                     通用 SwiftUI
agents/
  yolo-trade/runtime-python/    YoloTrade 私有运行时与 IBKR
  yolo-beni/runtime-python/     YoloBeni 私有运行时
apps/
  yolo-trade/                   YoloTrade macOS/iOS 客户端
  yolo-beni/                    YoloBeni macOS/iOS 客户端
```

共享层不得根据产品 ID、名称、目录或端口推断能力。产品身份、凭据、数据、Skills、Keychain 和运行状态分别隔离；旧目录不会被自动接管。

## 启动 Host

无真实模型的演示模式：

```sh
PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
  python3 -m yolo_trade --mode demo

PYTHONPATH=packages/core-python:agents/yolo-beni/runtime-python \
  python3 -m yolo_beni --mode demo
```

各产品独立的 ChatGPT Provider：

```sh
PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
  .venv/bin/python -m yolo_trade --mode chatgpt

PYTHONPATH=packages/core-python:agents/yolo-beni/runtime-python \
  .venv/bin/python -m yolo_beni --mode chatgpt
```

YoloTrade 默认端口为 8765，YoloBeni 为 8766。数据分别位于：

```text
~/Library/Application Support/YoloAgents/yolo-trade/
~/Library/Application Support/YoloAgents/yolo-beni/
```

YoloTrade 还保留显式 Codex 兼容模式：

```sh
PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
  .venv/bin/python -m yolo_trade --mode codex
```

该模式由 Codex 管理登录；产品不读取或复制 Codex token。独立 ChatGPT Provider 的设计与历史验收见 [归档方案](archive/CHATGPT_PROVIDER_PLAN.md)。

## 测试

开发 commit 不运行测试或构建；推送并创建 PR 后的 runner 门禁与授权合并规则统一见 [PR 测试 Skill](.agents/skills/yolo-agents-pr-test/SKILL.md)。下面的命令用于对应测试组或用户单独要求的检查，不作为 commit 门禁。

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python \
  .venv/bin/python -m unittest discover -s packages/core-python/tests -v

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python:agents/yolo-trade/runtime-python \
  .venv/bin/python -m unittest discover -s agents/yolo-trade/runtime-python/tests -v

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python:agents/yolo-beni/runtime-python \
  .venv/bin/python -m unittest discover -s agents/yolo-beni/runtime-python/tests -v

.venv/bin/python bin/test-agent-isolation.py
swift test --package-path packages/sdk-swift
swift test --package-path packages/ui-swift
swift test --package-path apps/yolo-trade/client-swift
swift test --package-path apps/yolo-beni/client-swift
```

当前验证基线、真实验收边界和下一步只维护在 [ROADMAP](docs/ROADMAP.md)，不在各 README 重复记录测试数量。

## 文档导航

- [架构与安全边界](docs/ARCHITECTURE.md)
- [当前状态与路线图](docs/ROADMAP.md)
- [开发与多会话协作规则](AGENTS.md)
- [PR 测试与合并门禁](.agents/skills/yolo-agents-pr-test/SKILL.md)
- [通信协议 v1](packages/protocol/COMMUNICATION_V1.md)
- [共享 Python Runtime](packages/core-python/README.md)
- [YoloTrade Runtime 与 IBKR](agents/yolo-trade/README.md)
- [YoloTrade Apple 客户端](apps/yolo-trade/README.md)
- [YoloBeni Runtime](agents/yolo-beni/README.md)
- [YoloBeni Apple 客户端](apps/yolo-beni/README.md)
- [主会话与上下文压缩](docs/PRIMARY_CONVERSATION.md)

历史调研、已完成方案和旧任务快照统一位于 [`archive/`](archive/)，不作为当前实现要求。
