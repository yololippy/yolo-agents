# Agents

每个子目录是一种可独立启动、配置和扩展的 Agent。它依赖 `packages/`，但不能把私有策略、账户数据或领域 SDK 反向放进共享包。

目录契约：

```text
agents/<agent-name>/
  README.md
  runtime-python/
    requirements.txt
    yolo_<agent-name>/
    tests/
```

当前有 `trading-agent/` 与 `b-agent/`。后者只保留目录契约，待业务范围确定后再实现。运行时目录按“职责-语言”命名；可跨 Agent 复用的 Host、协议与客户端 SDK 位于 `packages/`。
