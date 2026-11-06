---
name: yolo-agents-pr-test
description: "执行 yolo-agents 的 PR 后置测试与门禁检查。用于明确的 PR 语境，如“PR 测试”“PR 门禁”“运行这个 PR 的测试”“测试当前 PR”；不用于安装、升级或真实 Mac/iPhone 功能验收。"
---

# Yolo Agents PR 后置测试

本 Skill 是 PR 测试执行规则的唯一来源。开始前读取根目录 [AGENTS.md](../../../AGENTS.md)、[ROADMAP.md](../../../docs/ROADMAP.md)，并明确目标 PR 与产品；无法唯一确定时先询问，不改测另一产品。

交付顺序固定为：`功能分支 → commit → push → PR → 准备 → P0 → Mac 与 Simulator 并行 → 双端真实 UI 对话与同步 → 清理 → 获得合并授权的主会话 rebase merge`。只有前置阶段通过才进入后续阶段，禁止为了缩短墙钟时间越过顺序。runner 只测试和报告，不修改代码、不提交、不 push、不合并。

## 开发与提交

从最新 `origin/master` 创建 `feat/<topic>`、`fix/<topic>`、`refactor/<topic>`、`test/<topic>` 或 `docs/<topic>`。主分支受保护，不能直接提交或 push。

每次 commit 审查精确范围、暂存内容和差异格式，不运行测试配对门禁、测试或构建。功能与对应测试进入同一个 PR，可以分布在多个 commit。通用 commit-workflow 的测试时机由本仓库规则覆盖。

用户已授权本轮 push 与创建 PR 时，主会话连续完成提交、推送和创建 PR，不等待测试；创建的每个 PR 都附加到当前会话。未授权 push 时保持本地提交，不据此创建外部资源。

## 冻结待测版本

PR 创建或更新后，本机 runner 每 120 秒使用 GitHub 官方公开 REST API 检查一次受信任 PR。仅接受同仓库、非 fork、受信任作者、目标为 `master`、open 非 draft 且使用规定分支前缀的 PR；其他请求拒绝执行。元数据只包含版本标识，不包含认证信息或宿主连接凭据。

每个 PR 的私有 ledger 最多记录两轮。runner 在创建任何测试资源前原子预留轮次；同一 head 不重复执行，head 更新、runner 重启或中断都不会退还次数。第一轮未通过时只等待修复后的新 head；第二轮结束后无论结果和后续 push 均停止，不存在 reset、retry、force 或提高上限的配置。

runner 使用 `bin/yolo-agents-pr-gate` 准备测试运行。入口校验远端仓库、分支、版本与干净工作树，为 P0、Mac 和 Simulator 创建同一 head 的独立 detached worktree、构建目录和私有报告目录。开发 checkout 不承担测试中的修改或清理。

元数据文件只保留八个字段：`repository`、`pr_number`、`base_ref`、`base_sha`、`head_ref`、`head_sha`、`state`、`draft`。SHA 为官方 API 当前返回的完整 40 位小写值；`state` 必须为 `open`，`draft` 为 `false`。通过 Git SSH 获取 `origin/master` 和该 PR 的 `refs/pull/<编号>/head`，不复制浏览器或宿主的认证信息。

```sh
git fetch origin master refs/pull/<编号>/head:refs/pull/<编号>/head
bin/yolo-agents-pr-gate prepare --metadata <私有元数据.json> --product yolo-trade
```

prepare 返回本轮 `run_dir` 和 `run_id`，后续始终使用这两个值。运行文件位于 Git common directory 的私有门禁目录中，所有 worktree 共用锁；报告与构建路径不写入版本控制。

配对门禁使用 `bin/check-feature-tests --base <base-sha> --head <head-sha>`。它检查整个 PR 差异中的生产代码与同范围、未删除的测试代码，生产代码与测试可以在不同 commit；删除的测试不计入覆盖。它不读取暂存区，也不证明测试断言足够。

当前真实 UI 验收实现面向 YoloTrade；没有相应产品验收能力时必须报告 `blocked`，不能静默改测另一产品或把共享测试当成该产品验收。

## 分阶段自动测试

`bin/yolo-agents-pr-runner scan --config <私有配置>` 是唯一测试调度入口；LaunchAgent 每 120 秒启动一次，跨运行使用全局文件锁防止重复消费。`status` 只读取 ledger，不启动测试。配置只允许仓库、受信任作者、现有 checkout、Python、产品、产品凭据目录、私有状态目录和可选 PR 编号列表，不允许配置测试轮数。

runner 为每轮创建独立 source worktree，并按以下依赖调度受管子进程。执行和重试调度不依赖主会话、左侧栏测试会话或内部临时 Agent，它们只读取 runner 的脱敏状态与失败证据。总截止时间为 30 分钟；执行期间定期重新读取官方 PR 元数据，head、base、状态或 draft 变化会只中断本轮拥有的进程并记为 `superseded`，该轮仍计数。SIGINT/SIGTERM 同样只清理 runner 自己的进程、临时 Host、App、Simulator 和 worktree，不终止正式服务。

1. 准备完成后单独运行 P0。P0 未达到 `passed` 时，Mac、Simulator 和 UI 分别记为 `not_run`，并注明 P0 依赖未通过。
2. 只有 P0 报告验证为 `passed` 后，才并行运行 Mac 与 Simulator。任一未达到 `passed` 时，UI 记为 `not_run`，注明未通过的依赖；另一组中互不依赖的检查继续完成。
3. 只有 Mac 与 Simulator 均为 `passed`，且全部必需组报告验证通过后，才运行 UI。UI 未通过不生成可合并结论。

阶段结果使用 `passed`、`failed`、`blocked`、`not_run`；依赖未满足的阶段必须各自记录 `not_run`，不能省略报告或当作跳过通过。`superseded` 是本轮失效状态，不是通过结果。若现有 runner 不能遵守该顺序或报告契约，报告门禁 `blocked`，不得手动并行绕过或把要求描述成已经实现。

| 组 | 主要检查 |
| --- | --- |
| P0 | PR 全差异的功能/测试配对、测试入口自身回归、共享 Python、目标产品 Python、产品隔离、本地协议集成 |
| Mac | Swift SDK、共享 UI、目标产品客户端测试与独立 Mac 候选构建 |
| Simulator | 独立 Simulator 候选构建与 hosted SDK 测试 |

每组读取相同的运行标识、head SHA、base SHA 和产品，并写入自己的签名报告。runner 必须验证三个报告的签名、身份、完整检查清单、退出状态与日志哈希全部通过后，才启动 UI 验收；任何一组失败都不运行 UI，也不生成可合并结论。所有构建与日志使用本运行专属目录。

runner 先调用 P0 命令；报告验证通过后才并行调用后两条命令。`--root` 指向本轮冻结的 source worktree，入口从 manifest 选择该组自己的测试 worktree：

```sh
bin/yolo-agents-pr-gate --root <source-worktree> run-group --run-dir <run_dir> --group p0
bin/yolo-agents-pr-gate --root <source-worktree> run-group --run-dir <run_dir> --group mac
bin/yolo-agents-pr-gate --root <source-worktree> run-group --run-dir <run_dir> --group simulator
```

组内一个检查失败后仍完成其余互不依赖的检查。超时、缺失工具、缺少账号授权、测试被跳过和未执行都不能记为通过。dry run 只表示计划，不生成可合并结果。

## 实际 App 对话与同步

三个组通过后，runner 自动运行 `bin/test_yolo_trade_pr_ui.py`。该入口启动真实候选 Mac App 和一次性 iOS Simulator App，由 XCUITest 操作可见的会话、输入框和发送按钮，并核对双方的真实助手回复。

```sh
<现有venv的python> bin/test_yolo_trade_pr_ui.py \
  --run-dir <run_dir>/ui --run-id <run_id> \
  --head-sha <head-sha> --base-sha <base-sha> \
  --python <现有venv的python> --credentials-data-dir <产品私有data目录>
```

从本轮冻结且干净的 source worktree 调用，脚本与所有候选源码必须来自该 head。凭据来源是带产品所有权标记的完整私有数据目录，不接受用户输入 token 或任意凭据文件；入口自行验证，报告不记录其中内容。

两端连接同一个仅监听 loopback 的临时 YoloTrade Host。临时 Host 使用 YoloTrade 自己已正式授权的官方 Provider；凭据保留在产品专用存储中，不复制到测试目录、App、报告或 PR。运行时不注册交易工具，不加载 Skills、记忆或生产会话；临时聊天 API 不提供券商或订单能力。

Mac 和 Simulator 分别使用自己的临时配对身份，共用测试会话。必须完成：Mac 发送并收到真实模型回复；Simulator 读取 Mac 的消息与回复并发起自己的真实往返；Mac 再读取 Simulator 的消息与回复。仅模型探针、HTTP 请求受理、SDK 测试、App 启动或 UI 中的固定假回复都不能满足这项检查。

测试 Bundle ID、Keychain service、缓存和数据目录与正式客户端隔离。不得替换或终止正式 Mac App、重启生产 Host/Gateway、读取正式客户端配对缓存、安装真实 iPhone，或改变 Paper 执行配置。正式服务的配置保持不变；Provider 令牌的正常轮换继续遵守产品已有跨进程凭据租约。

同一产品的真实验收通过跨 worktree 的运行锁串行执行。成功或失败都应终止临时 Host、测试 App，清理一次性 Simulator 和测试凭据，保留脱敏结果。报告不包含配对码、设备 token、Provider 凭据或私人聊天。包含启动秘密的中间产物不能上传至公开 PR。

## 汇总与合并

runner 从官方 GitHub API 重新取得当前 PR 和 master 元数据，再调用门禁汇总。所有必需报告都必须对应本运行的同一产品、head/base，并且实际执行结果为 `passed`。报告缺失、重复、版本不符、工作树污染、失败、受阻、跳过或 dry run 都拒绝放行。

```sh
bin/yolo-agents-pr-gate summary --run-dir <run_dir> \
  --metadata <刚重新取得的私有元数据.json> --ui-report <run_dir>/ui/report.json
```

汇总要求元数据是在全部检查结束后重新取得、且不超过 120 秒的快照。只允许 `summary.json` 中 `merge_ready=true` 且收尾完整的本轮结果进入合并步骤。

head 或 master 在测试期间变化时，本轮结果失效，不能只替换报告中的 SHA 或沿用旧结果。仅当仍有未消费的轮次且 head 未测试过时，才允许重新冻结新版本进入下一轮；base 单独变化不构成重跑同一 head 或清零账本的例外。不能通过手工运行门禁、删除 ledger、重建状态目录或换 runner 绕过每 PR 两轮上限。

runner 在每轮成功、失败或中断后统一调用 `bin/yolo-agents-pr-gate cleanup --run-dir <run_dir>`。清理本轮 source worktree、三个测试 worktree 以及各组受管 `build/` 构建产物；仅删除本运行创建且仍干净的 worktree，不强制删除异常改动。保留私有 ledger、签名 manifest、签名检查报告、日志、UI 报告和汇总收据。清理失败必须记录 `cleanup=incomplete`、残留路径与原因，阻止放行，不得把残留产物当作可复用缓存或清零账本。

汇总允许合并后，runner 只报告就绪并停止。主会话合并前重新读取 GitHub PR 与 master，核对同一轮所有必需组和 UI 均为 `passed`、head/base 未变化、工作树未污染、清理完整、PR 无冲突且没有未满足的 GitHub 门禁或要求修改的 review。只有用户已授权本轮合并时，才使用 GitHub 官方接口的 `rebase` 方法，传入预期 head SHA；再次核对 base 后立即执行。保留每个 commit，不 squash、不创建 merge commit；没有合并授权时只报告就绪，不从开发或 commit 请求推断授权。GitHub 若因并发更新或保护规则拒绝，重新读取状态，不绕过门禁或强推。

## 耗时与交付报告

本轮执行耗时统一按 `准备 + P0 + max(Mac, Simulator) + UI + 清理` 计算。准备包含本轮资源与冻结版本的准备；Mac 与 Simulator 各自按实际起止计时，并行段只计较慢的一组，不能把两组相加，也不能与 P0 或 UI 重叠来缩短耗时。未执行阶段时长为零，同时保留 `not_run` 与依赖原因，不宣称该阶段已验证。

报告分别列出准备、P0、Mac、Simulator、UI、清理的起止时间、时长、状态及证据位置，给出上述总耗时，并列出各阶段的慢项检查及其耗时。扫描等待、锁等待或失败后等待修复的时间如有发生应单列；不混入检查耗时，也不得隐去实际墙钟跨度。汇报测试结论、失败与未执行原因、已消费轮次、清理状态和合并就绪状态；日志与报告继续脱敏。

PR runner 是本机交付门禁服务，只监测配置中受信任的仓库与 PR，不执行产品周期任务。用户明确要求的完整安装、升级、回滚和真机流程使用 [完整测试技能](../yolo-agents-full-test/SKILL.md)，两种入口不能互相替代。
