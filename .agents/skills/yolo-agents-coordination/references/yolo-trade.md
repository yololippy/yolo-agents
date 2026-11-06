# YoloTrade

`main-trade` 以 YoloTrade 为主要产品上下文，通常协调 `agents/yolo-trade/` 与 `apps/yolo-trade/`，但具备与 `main` 相同的仓库修改、验收和 Git 能力。

- `Trade IBKR` 负责券商接入、账户事实、订单、对账和交易规则，写入范围由任务进一步限定到具体文件或子模块。
- `Trade App` 负责 YoloTrade 私有客户端、产品入口和非共享界面。
- 券商凭据、账户数据、风控、订单和运行状态始终属于 YoloTrade，不因 YoloBeni 出现而进入共享层。
- YoloTrade 当前持久运行配置是 Paper 可执行：`environment=paper`、`read_only=false`、`execution_enabled=true`。除非用户明确撤销，所有部署、修复、升级和迁移都必须保留，不得改回只读或反复询问该产品级偏好。
- Paper 可执行不等于订单免确认。模型不能自行提交；Mac 管理设备可以单独授权或撤销已配对 iPhone 的 Paper 交易权限，但每笔订单仍须由发起设备明确确认，并经过 Mac Host 的提交前复核、硬风控、账户级串行、幂等和 unknown 不重发门禁。Mac Host 是唯一券商执行端，Live 执行仍关闭。
- IBKR 运维任务必须同时核验 8765 Host、本地 Gateway 接口、IBKR 上游和数据就绪；保留 07:30 Auto Restart、被动监听检查、单实例门禁，以及 `1100`／`2110` 的维护窗口、15 分钟宽限和每轮最多一次 Gateway 重启策略。
- 不在 Skill、AGENTS、文档或其他版本控制文件中记录实际账户标识和 Gateway 凭据；账户只进入 YoloTrade 私有本机配置，用户名和密码只进入产品专用 Keychain。
- 同一私有文件只能由一个会话修改；需要共享接口时，先由 `main-trade` 提出边界，再按共享层规则登记唯一所有者。
- 产品上下文不是硬性文件权限边界；当前用户任务需要时，`main-trade` 可以修改和提交共享层或其他仓库文件。
- `main-trade` 只能调用自己的直属普通开发会话，不得调用、联系、等待或移交给 `main`、`main-beni`。
