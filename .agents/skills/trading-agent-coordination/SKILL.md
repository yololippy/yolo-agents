---
name: trading-agent-coordination
description: "在 yolo-agents 仓库中，由 main 协调已有的 Ibkr、Agent、iOS 会话开发。用于跨会话派活、收取结果和集成验证；不用于应用内的交易调度。"
---

# Trading Agent 协作

## 已确定的工作方式

用户只与 `main` 对话，由 main 给同仓库的 `Ibkr`、`Agent`、`iOS` 三个已有会话指派工作、收取结果并整合。用户已经授权这种协调方式，在该范围内无需重复询问。

用户已澄清：项目代码依赖可以直接安装，Python 优先使用仓库 `.venv`；安装完整 Xcode、IB Gateway、远程网络客户端等独立软件时才叫用户。其他常规开发、测试和可逆操作自主推进，不重复确认，不绕过系统或服务的访问限制。

读取 [项目大纲](../../../docs/PROJECT_OUTLINE.md)、[现有 TODO](../../../docs/TODO.md) 和 [Agent 方案](../../../docs/RESEARCH_PYDANTICAI.md)。IBKR 路线是 IB Gateway／TWS API 的独立 Python 薄服务，优先验证 ib_async；主 Agent 使用 PydanticAI Core 与官方 Harness 组件。用户已确定的选型不因旧文档仍写“暂缓”而重新要求确认。当前任务和用户最新指令优先。

## 派活与收取结果

- 用 `list_threads` 按项目和工作目录识别现有会话，保留实际返回的标题；标题不唯一时再核实。不要为这些已有角色创建替代会话。
- `Ibkr` 负责券商接入、账户事实、订单、对账与交易规则；`Agent` 负责 PydanticAI、会话、记忆与受控工具；`iOS` 负责 Apple 客户端和同步体验。按实际任务明确接口、交付、验证与文件归属，三者共享工作目录，避免同时修改同一文件。
- 通过 `send_message_to_thread` 向授权的会话发出具体工作指令；用 `wait_threads` 读取结果并跟踪后续变化，需要细节时再读会话。main 负责集成验证和向用户报告；不要求对方主动向 main 发消息。
- 不增加用于中断恢复的业务进度文件；沿用会话上下文和已有项目文档。正常开发如需更新现有 TODO，按当前任务处理。
