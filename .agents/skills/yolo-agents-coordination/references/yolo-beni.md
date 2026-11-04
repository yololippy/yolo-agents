# YoloBeni

`main-beni` 负责 YoloBeni 产品内协调，默认私有范围是 `agents/yolo-beni/` 与 `apps/yolo-beni/`。

- `Beni Runtime` 负责生活助手的私有领域逻辑、个人偏好、受控工具和服务入口。
- `Beni App` 负责 YoloBeni 的 Mac、iPhone、Xcode 工程和产品专属界面。
- 日历、提醒、地图、健康等能力初次只有 YoloBeni 使用时保留在产品私有层；出现真实跨产品需求后再按共享层规则抽取。
- 个人数据、授权、凭据、缓存、Skills 和运行状态按 `yolo-beni` 身份隔离，不能读取或复用 YoloTrade 私有数据。
- `main-beni` 负责产品内审阅和验证，但不执行 Git 操作；完成后向 `main` 报告并交付集成。
