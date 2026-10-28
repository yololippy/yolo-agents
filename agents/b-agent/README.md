# B Agent

独立的聊天 Agent 骨架，复用共享 PydanticAI/Harness 与 ChatGPT Provider，以 `B Agent` 的应用名称授权。没有 IBKR、订单工具或 Trading Agent 私有依赖。

从仓库根目录启动：

```sh
PYTHONPATH=packages/core-python:agents/b-agent/runtime-python .venv/bin/python -m yolo_b_agent --mode chatgpt
```

默认端口 `8766`，数据目录 `~/Library/Application Support/YoloAgents/b-agent/`。无模型调用的隔离演示使用 `--mode demo`。代码依赖见 `runtime-python/requirements.txt`，精确依赖继承共享 lock 文件。

Mac 和 iOS App 属于同一个 B Agent，配对此后端后共享聊天与记忆；授权凭据只留在 Mac 后端。客户端入口见 [apps/b-agent](../../apps/b-agent/README.md)。数据、授权与 Skills 隔离的完整规则见 [Provider 方案](../../docs/CHATGPT_PROVIDER_PLAN.md)。

当前业务范围只包含聊天基础与授权，真实模型和用户浏览器授权需要单独验收。
