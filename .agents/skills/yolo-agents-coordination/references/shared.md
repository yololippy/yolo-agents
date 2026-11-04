# 共享层

共享范围包括 `packages/protocol/`、`packages/core-python/`、`packages/sdk-swift/` 和 `packages/ui-swift/`。

- 只有已经被多个产品真实复用，或属于稳定跨产品协议的能力才进入 `packages/`；单一产品的策略、账户、集成和界面留在产品目录。
- 每个共享任务必须登记唯一所有者：`main`、`main-trade` 或 `main-beni`。只有所有者可以派发对应共享开发会话并收取结果。
- `Shared Core` 默认负责协议与 Python 运行时；`Shared Apple` 默认负责 Swift SDK 与通用 UI。具体文件范围仍须逐次派发，不能只按顶层目录笼统授权。
- 共享接口变更先确定调用方、兼容方式和修改顺序；涉及两个产品时由 `main` 仲裁并完成最终集成。
- 验证至少覆盖受影响的共享测试和当前接入产品；不为了证明复用性而扩展未在本次范围内的产品功能。
