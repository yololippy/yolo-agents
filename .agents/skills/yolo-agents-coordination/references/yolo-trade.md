# YoloTrade

`main-trade` 以 YoloTrade 为主要产品上下文，通常协调 `agents/yolo-trade/` 与 `apps/yolo-trade/`，但具备与 `main` 相同的仓库修改、验收和 Git 能力。

- `Trade IBKR` 负责券商接入、账户事实、订单、对账和交易规则，写入范围由任务进一步限定到具体文件或子模块。
- `Trade App` 负责 YoloTrade 私有客户端、产品入口和非共享界面。
- 券商凭据、账户数据、风控、订单和运行状态始终属于 YoloTrade，不因 YoloBeni 出现而进入共享层。
- 同一私有文件只能由一个会话修改；需要共享接口时，先由 `main-trade` 提出边界，再按共享层规则登记唯一所有者。
- 产品上下文不是硬性文件权限边界；当前用户任务需要时，`main-trade` 可以修改和提交共享层或其他仓库文件。
- `main-trade` 只能调用自己的直属普通开发会话，不得调用、联系、等待或移交给 `main`、`main-beni`。
