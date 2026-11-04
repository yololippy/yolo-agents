# YoloTrade

`main-trade` 负责 YoloTrade 产品内协调，默认私有范围是 `agents/yolo-trade/` 与 `apps/yolo-trade/`。

- `Trade IBKR` 负责券商接入、账户事实、订单、对账和交易规则，写入范围由任务进一步限定到具体文件或子模块。
- `Trade App` 负责 YoloTrade 私有客户端、产品入口和非共享界面。
- 券商凭据、账户数据、风控、订单和运行状态始终属于 YoloTrade，不因 YoloBeni 出现而进入共享层。
- 同一私有文件只能由一个会话修改；需要共享接口时，先由 `main-trade` 提出边界，再按共享层规则登记唯一所有者。
- `main-trade` 负责产品内审阅和验证，但不执行 Git 操作；完成后向 `main` 报告并交付集成。
