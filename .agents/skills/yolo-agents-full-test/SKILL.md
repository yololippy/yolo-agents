---
name: yolo-agents-full-test
description: "执行 yolo-agents 的独立完整测试，包括自动化回归、升级替换、真实 Mac/iPhone 安装和产品功能验收。只在用户明确要求完整测试时使用；不用于普通开发后的相关测试、单项测试或代码审查。"
---

# Yolo Agents 完整测试

这是用户明确触发的后置流程。普通功能开发、修复、review、构建或 commit 不自动进入本流程。

## 开始条件

1. 读取仓库根目录 `AGENTS.md`、`docs/ROADMAP.md` 和目标产品参考。YoloTrade 读取 `../yolo-agents-coordination/references/yolo-trade.md`；涉及 iPhone 操作时同时读取 `../yolo-agent-device-pairing/SKILL.md`。
2. 明确目标产品。无法从用户指令和当前产品上下文唯一确定时，停止并询问，不猜测。
3. 记录当前 Git revision、未提交差异、已安装版本、目标设备和运行状态。报告可以测试包含未提交改动的候选版本，但必须明确其差异指纹。
4. 为本轮创建 `.agents/runtime/yolo-agents-full-test/` 下的运行锁和脱敏报告。已有活动锁时停止，避免两个会话同时安装、重启或配对同一产品。

## 执行顺序

按以下阶段顺序执行。已有固定脚本时优先调用固定脚本，并把命令、产物和结果写入本轮报告：

1. `automated`：运行 `bin/yolo-agents-full-test automated --product <产品 ID>`，执行共享层与目标产品的单元、契约、集成测试和 Apple 构建。
2. `upgrade`：使用明确提供的旧版基线与当前候选产物，运行 `bin/yolo-agents-full-test upgrade --product <产品 ID> --baseline-macos-app <旧版 Mac.app> --candidate-macos-app <候选 Mac.app> --baseline-ios-app <旧版 Simulator.app> --candidate-ios-app <候选 Simulator.app>`，在隔离目录和一次性 Simulator 执行升级替换测试。没有可靠旧版基线时仍运行入口并将本阶段标为受阻，不得以当前版本的两份副本冒充升级。
3. `live`：只有前两个阶段的必需检查通过后，升级真实 Mac 和 iPhone。Mac 必须整包替换并保留可恢复备份；iOS Bundle ID 不一致属于身份迁移，停止普通覆盖安装。
4. `acceptance`：完成安装后的真实功能验收。包含 AI 的产品必须使用产品自己的授权完成无业务副作用的真实模型往返。

阶段结果只有 `passed`、`failed`、`blocked`、`not_run`。跳过、超时、运行旧产物或只收到请求受理均不得记为通过。依赖失败项的后续阶段不执行；其余互不依赖检查继续完成。

## YoloTrade 门槛

- 保留 Paper、`read_only=false`、`execution_enabled=true`；不得据此提交任何具体订单。
- 真实验收要求 Host 身份正确，Mac/iPhone 在线与同步，Provider 已授权、模型目录可用且真实回复成功。
- Broker 必须为 `connected_paper`、`ready=true`、`data_complete=true`，且没有 `submitting`、`unknown`、`cancel_pending` 未决订单。
- 模型往返在执行层禁用业务工具。不得仅靠提示词要求模型不要下单。

## 失败与报告

- 默认只诊断和报告，不修改产品代码、不调整风控、不清空数据、不提交 Git。本轮替换失败后的恢复和临时资源清理属于测试收尾。
- 需要密码、2FA、通行密钥、生物识别或系统确认时，精确说明唯一需要用户完成的动作并等待。
- 报告必须分别给出自动化回归、升级替换、Mac 实际可用、iPhone 实际可用和产品功能验收结论，并列出失败证据、受阻原因及未执行项。
- 报告不得包含账户号、配对码、设备 token、OAuth token 或其他凭据。
