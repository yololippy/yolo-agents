# Yolo Agents

运行在个人 Mac 上、可扩展为多个 Agent 的本地 Agent Host。交易 Agent 是第一个实例；Mac 和 iOS App 通过本机服务连接它。

```text
packages/                       跨 Agent/App 的可复用包
  protocol/                     通信协议与数据模型
  core-python/                  Host、认证、存储和通用运行时
  sdk-swift/                    Swift 通信 SDK 与联调 probe
  ui-swift/                     可复用 SwiftUI 聊天界面
agents/                         各 Agent 的领域运行时
  trading-agent/runtime-python/ 第一个 Agent：IBKR Paper 交易集成
  b-agent/runtime-python/       第二个 Agent 的扩展骨架
apps/                           各 Agent 的用户客户端
  trading-agent/client-swift/    Trading Agent 私有的双端客户端模块
  trading-agent/{ios,macos,xcode}/
  b-agent/{ios,macos,xcode}/
```

## 本轮可运行内容

- Python 后端：持久会话、消息幂等、串行回复、设备配对/撤销、增量事件及 SSE。
- Agent：明确演示模式、PydanticAI Core / Harness 适配，以及复用 Codex 受管 ChatGPT 登录的真实聊天模式。Codex 模式已实测模型目录、流式生成、模型切换和服务重启续聊；PydanticAI 的真实云供应商仍需另行配置与验收。
- Apple：共享 Swift 通信 SDK、Keychain 与离线同步状态、SwiftUI macOS / iOS 客户端及 Xcode 工程。本机已用完整 Xcode 27.1 完成 iOS Simulator arm64 构建；Mac 客户端已实测新会话、模型选择、输入和真实 Codex 流式回复。
- IBKR 已安装并登录 Paper Gateway；交易 Agent 已实现 Paper 专用的启动对账、What-if、Mac 明确确认后单次提交/单笔撤单、独立订单账本及 iOS 只读订单界面。Live 始终拒绝执行。本地模拟覆盖已完成；真实 Gateway 当前仍启用 Read-Only API，因此尚未进行真实 What-if 或模拟订单验收。

## 先运行后端演示

现有 Python 3.11+ 即可，不需要安装第三方依赖：

```sh
PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python python3 -m yolo_trading --mode demo
```

默认地址 `http://127.0.0.1:8765`。控制台提供两个十分钟有效的一次性配对码，分别给 Mac / iOS 使用；配对成功后客户端将设备 token 保存到 Keychain。演示回复会明确说明没有调用真实模型或 IBKR。

已在 Mac 的 Codex 中完成 ChatGPT 登录时，可以启动真实 Codex 聊天模式：

```sh
PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python .venv/bin/python -m yolo_trading --mode codex
```

该模式由 Codex 管理登录和刷新，应用不读取或复制登录 token；详情见 [Codex 接入说明](packages/core-python/CODEX.md)。

- [共享 Host、存储与真实模型适配边界](packages/core-python/HOST.md)
- [共享 Agent 运行时说明](packages/core-python/README.md)
- [交易 Agent 与 IBKR Paper 配置](agents/trading-agent/README.md)
- [交易 Agent 的 Apple 客户端](apps/trading-agent/README.md)
- [通信协议 v1](packages/protocol/COMMUNICATION_V1.md)
- [需求与架构](docs/PROJECT_OUTLINE.md)
- [开发任务](docs/TODO.md)
- [Agent 设计与验证边界](docs/RESEARCH_PYDANTICAI.md)

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python .venv/bin/python -m unittest discover -s packages/core-python/tests -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/core-python:agents/trading-agent/runtime-python .venv/bin/python -m unittest discover -s agents/trading-agent/runtime-python/tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s bin/tests -v
python3 bin/test-chat-integration.py --probe
swift test --package-path packages/sdk-swift
swift test --package-path packages/ui-swift
swift test --package-path apps/trading-agent/client-swift
```

当前两套 Python 测试共 190 项通过（共享 Core 79、交易 Agent 111）；共享 Swift SDK 共 43 项（42 通过、1 项真实服务测试默认跳过），共享 SwiftUI 17 项、Trading Agent 客户端 21 项通过。Mac arm64 与 iOS Simulator Debug 构建成功；Mac App 已完成真实 Codex 模型目录、模型切换、新会话和 120 个增量事件的流式回复验收。Paper 订单目前仅完成内存模拟与构建验证；Gateway 可写权限、真实 What-if、模拟成交/撤单、外网弱网及 iPhone 真机交互仍须分别验收。
