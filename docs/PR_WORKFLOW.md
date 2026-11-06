# PR 后置测试与合并

开发顺序固定为：`功能分支 → commit → push → PR → 并行测试 → 双端真实 UI 对话与同步 → rebase merge`。commit 保存开发进度，测试决定 PR 能否合并。

## 开发与提交

从最新 `origin/master` 创建 `feat/<topic>`、`fix/<topic>`、`refactor/<topic>`、`test/<topic>` 或 `docs/<topic>`。主分支受保护，不能直接提交或 push。

每次 commit 审查精确范围、暂存内容和差异格式，不运行测试配对门禁、测试或构建。功能与对应测试进入同一个 PR，可以分布在多个 commit。通用 commit-workflow 的测试时机由本仓库规则覆盖。

用户已授权本轮 push 与创建 PR 时，主会话连续完成提交、推送和创建 PR，不等待测试；创建的每个 PR 都附加到当前会话。未授权 push 时保持本地提交，不据此创建外部资源。

## 冻结待测版本

创建 PR 后，主会话立即从官方 GitHub API 读取 PR 元数据。记录仓库、PR 编号、产品、head/base 分支及完整 SHA、open/draft 状态。元数据只包含版本标识，不包含认证信息。

使用 `bin/yolo-agents-pr-gate` 准备测试运行。入口校验远端仓库、分支、版本与干净工作树，为 P0、Mac 和 Simulator 创建同一 head 的独立 detached worktree、构建目录和私有报告目录。准备阶段由主会话执行；开发 checkout 不承担测试中的修改或清理。

元数据文件只保留八个字段：`repository`、`pr_number`、`base_ref`、`base_sha`、`head_ref`、`head_sha`、`state`、`draft`。SHA 为官方 API 当前返回的完整 40 位小写值；`state` 必须为 `open`，`draft` 为 `false`。通过 Git SSH 获取 `origin/master` 和该 PR 的 `refs/pull/<编号>/head`，不复制浏览器或宿主的认证信息。

```sh
git fetch origin master refs/pull/<编号>/head:refs/pull/<编号>/head
bin/yolo-agents-pr-gate prepare --metadata <私有元数据.json> --product yolo-trade
```

prepare 返回本轮 `run_dir` 和 `run_id`，后续始终使用这两个值。运行文件位于 Git common directory 的私有门禁目录中，所有 worktree 共用锁；报告与构建路径不写入版本控制。

配对门禁使用 `bin/check-feature-tests --base <base-sha> --head <head-sha>`。它检查整个 PR 的差异，生产代码与测试可以在不同 commit；删除的测试不计入覆盖。它不读取暂存区，也不证明测试断言足够。

当前真实 UI 验收实现面向 YoloTrade；没有相应产品验收能力时必须报告 `blocked`，不能静默改测另一产品或把共享测试当成该产品验收。

## 并行测试

主会话创建或复用最多三个左侧栏可见的独立测试会话。不得用当前会话内部的临时子 Agent 代替。

用户已选择所有直属开发、测试会话使用 Full Access（`danger-full-access`、`approval_policy=never`）。按 [协作技能](../.agents/skills/yolo-agents-coordination/SKILL.md) 显式配置每次启动/恢复的权限，并核对准确 worktree；应用创建工具没有权限参数时，用等待初始化的侧栏会话配合官方 CLI `exec resume`，不能只在任务文字中声明权限。

| 组 | 主要检查 |
| --- | --- |
| P0 | PR 全差异的功能/测试配对、测试入口自身回归、共享 Python、目标产品 Python、产品隔离、本地协议集成 |
| Mac | Swift SDK、共享 UI、目标产品客户端测试与独立 Mac 候选构建 |
| Simulator | 独立 Simulator 候选构建与 hosted SDK 测试 |

每组读取相同的运行标识、head SHA、base SHA 和产品，并写入自己的报告。测试会话只运行与报告，不能改代码、提交、push、继续派发或重试业务请求；固定入口可以执行只读 Git 命令核对版本。所有构建与日志使用本运行专属目录。

三个测试会话分别调用一条命令；`--root` 指向主会话已冻结的开发 worktree，入口从 manifest 选择该组自己的测试 worktree：

```sh
bin/yolo-agents-pr-gate --root <开发worktree> run-group --run-dir <run_dir> --group p0
bin/yolo-agents-pr-gate --root <开发worktree> run-group --run-dir <run_dir> --group mac
bin/yolo-agents-pr-gate --root <开发worktree> run-group --run-dir <run_dir> --group simulator
```

组内一个检查失败后仍完成其余互不依赖的检查。超时、缺失工具、缺少账号授权、测试被跳过和未执行都不能记为通过。dry run 只表示计划，不生成可合并结果。

## 实际 App 对话与同步

三个组完成后，主会话运行 `bin/test_yolo_trade_pr_ui.py`。该入口启动真实候选 Mac App 和一次性 iOS Simulator App，由 XCUITest 操作可见的会话、输入框和发送按钮，并核对双方的真实助手回复。

```sh
<现有venv的python> bin/test_yolo_trade_pr_ui.py \
  --run-dir <run_dir>/ui --run-id <run_id> \
  --head-sha <head-sha> --base-sha <base-sha> \
  --python <现有venv的python> --credentials-data-dir <产品私有data目录>
```

从冻结且干净的开发 worktree 调用，脚本与所有候选源码必须来自该 head。凭据来源是带产品所有权标记的完整私有数据目录，不接受用户输入 token 或任意凭据文件；入口自行验证，报告不记录其中内容。

两端连接同一个仅监听 loopback 的临时 YoloTrade Host。临时 Host 使用 YoloTrade 自己已正式授权的官方 Provider；凭据保留在产品专用存储中，不复制到测试目录、App、报告或 PR。运行时不注册交易工具，不加载 Skills、记忆或生产会话；临时聊天 API 不提供券商或订单能力。

Mac 和 Simulator 分别使用自己的临时配对身份，共用测试会话。必须完成：Mac 发送并收到真实模型回复；Simulator 读取 Mac 的消息与回复并发起自己的真实往返；Mac 再读取 Simulator 的消息与回复。仅模型探针、HTTP 请求受理、SDK 测试、App 启动或 UI 中的固定假回复都不能满足这项检查。

测试 Bundle ID、Keychain service、缓存和数据目录与正式客户端隔离。不得替换或终止正式 Mac App、重启生产 Host/Gateway、读取正式客户端配对缓存、安装真实 iPhone，或改变 Paper 执行配置。正式服务的配置保持不变；Provider 令牌的正常轮换继续遵守产品已有跨进程凭据租约。

同一产品的真实验收通过跨 worktree 的运行锁串行执行。成功或失败都应终止临时 Host、测试 App，清理一次性 Simulator 和测试凭据，保留脱敏结果。报告不包含配对码、设备 token、Provider 凭据或私人聊天。包含启动秘密的中间产物不能上传至公开 PR。

## 汇总与合并

主会话从官方 GitHub API 重新取得当前 PR 和 master 元数据，再调用门禁汇总。所有必需报告都必须对应本运行的同一产品、head/base，并且实际执行结果为 `passed`。报告缺失、重复、版本不符、工作树污染、失败、受阻、跳过或 dry run 都拒绝放行。

```sh
bin/yolo-agents-pr-gate summary --run-dir <run_dir> \
  --metadata <刚重新取得的私有元数据.json> --ui-report <run_dir>/ui/report.json
```

汇总要求元数据是在全部检查结束后重新取得、且不超过 120 秒的快照。只允许 `summary.json` 中 `merge_ready=true` 的本轮结果进入合并步骤。收尾由主会话运行 `bin/yolo-agents-pr-gate cleanup --run-dir <run_dir>`；入口只移除本运行创建且仍干净的测试 worktree，保留报告，不强制删除异常改动。

head 或 master 在测试期间变化时，本轮结果失效。重新冻结新版本、准备新的运行并测试，不能只替换报告中的 SHA，也不能沿用旧结果。

汇总允许合并后，主会话还需核对 PR 无冲突、没有未满足的 GitHub 门禁或要求修改的 review，并确认用户已授权合并。使用 GitHub 官方接口的 `rebase` 方法，传入预期 head SHA；再次核对 base 后立即执行。GitHub 若因并发更新或保护规则拒绝，重新读取状态，不绕过门禁、强推或降级为 squash/merge commit。

PR 后置测试是交付门禁，不是周期性任务，也不会自动安装后台服务。用户明确要求的完整安装、升级、回滚和真机流程仍使用 [完整测试技能](../.agents/skills/yolo-agents-full-test/SKILL.md)。
