# Trading Agent

运行在个人 Mac 上的对话式 IBKR 助手，Apple 客户端与本机后台服务分离。当前先实现聊天、持久化与跨设备通信，券商和交易能力后续接入。

## 本轮可运行内容

- Python 后端：持久会话、消息幂等、串行回复、设备配对/撤销、增量事件及 SSE。
- Agent：明确演示模式，以及 PydanticAI Core / Harness 适配。依赖已装入 `.venv`，官方 SDK 离线模型已验证真实记忆工具、历史召回、压缩和会话恢复；实际云端模型仍需供应商配置与联调。
- Apple：共享 Swift 通信 SDK、Keychain 与离线同步状态、SwiftUI macOS / iOS 客户端及 Xcode 工程。本机已安装完整 Xcode 27.1，iOS 模拟器版本构建通过。
- IBKR 已有显式 paper/live 的本机只读配置与适配层，支持安全切换并在健康接口中显示脱敏状态；当前未安装/登录 Gateway，尚无账户连接、账户数据或订单执行。

## 先运行后端演示

现有 Python 3.11+ 即可，不需要安装第三方依赖：

```sh
PYTHONPATH=server python3 -m trading_agent --mode demo
```

默认地址 `http://127.0.0.1:8765`。控制台提供两个十分钟有效的一次性配对码，分别给 Mac / iOS 使用；配对成功后客户端将设备 token 保存到 Keychain。演示回复会明确说明没有调用真实模型或 IBKR。

- [后端启动与 HTTPS 配置](server/README.md)
- [Agent、存储与真实模型适配边界](server/AGENT.md)
- [Apple 客户端](apple/README.md)
- [通信协议 v1](docs/COMMUNICATION_V1.md)
- [IBKR 配置与只读接入](server/IBKR.md)
- [需求与架构](docs/PROJECT_OUTLINE.md)
- [开发任务](docs/TODO.md)
- [Agent 设计与验证边界](docs/RESEARCH_PYDANTICAI.md)

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=server .venv/bin/python -m unittest discover -s server/tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s bin/tests -v
python3 bin/test-chat-integration.py --probe
cd apple
swift test
```

本轮已通过后端 65 项测试、完整 Xcode 下的 25 项 Swift SDK 单元测试，以及 iPhone 17 Pro / iOS 27.0 模拟器中的 27 项测试。模拟器测试覆盖真实 Keychain 读写和原生 SDK→Python 演示服务的中文消息、幂等重试、事件补齐与设备撤销；后端另有证书校验开启的本地 HTTPS 验证。已修复 Agent 首次并发初始化的存储竞争和流提前关闭的资源清理问题，并验证服务正常启动/退出。模拟器使用本地临时签名，无需开发者账号，运行步骤见 Apple 说明。云端供应商、外网连接和 iPhone 真机分别验收，离线模型通过不代表真实模型决策、券商或真机已经验证。
