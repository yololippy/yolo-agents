# YoloBeni

`main-beni` 以 YoloBeni 为主要产品上下文，通常协调 `agents/yolo-beni/` 与 `apps/yolo-beni/`，但具备与 `main` 相同的仓库修改、验收和 Git 能力。

- `Beni Runtime` 负责生活助手的私有领域逻辑、个人偏好、受控工具和服务入口。
- `Beni App` 负责 YoloBeni 的 Mac、iPhone、Xcode 工程和产品专属界面。
- 日历、提醒、地图、健康等能力初次只有 YoloBeni 使用时保留在产品私有层；出现真实跨产品需求后再按共享层规则抽取。
- 个人数据、授权、凭据、缓存、Skills 和运行状态按 `yolo-beni` 身份隔离，不能读取或复用 YoloTrade 私有数据。
- 产品上下文不是硬性文件权限边界；当前用户任务需要时，`main-beni` 可以修改和提交共享层或其他仓库文件。
- `main-beni` 只能调用自己的直属普通开发会话，不得调用、联系、等待或移交给 `main`、`main-trade`。
