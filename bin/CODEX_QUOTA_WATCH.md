# Codex 额度恢复监控

本工具监控 Codex 套餐额度，并在恢复后向当前 main 会话发送一条“继续”。应用服务、IBKR 和模型 API 均不受它控制。

## 文件位置

- `bin/codex-quota-watch.py`：额度读取、定时与恢复逻辑。
- `bin/start-codex-quota-watch.py`：后台启动脚本。
- `bin/stop-codex-quota-watch.py`：优雅停止脚本。
- `runtime/codex-quota-watch/`：本机运行时目录，保存日志、PID 与停止标记；整个目录已被 Git 忽略。

## 恢复节奏

监控发现额度受限后，从接口的 `primary.resetsAt` 读取下次主额度重置时间；主窗口没有该字段时才使用 `secondary.resetsAt`。

它在重置时间后一小时执行首次恢复尝试。每次尝试先重新读取额度，只有 `ordinaryUsageAllowed` 明确为 `true` 才会发送“继续”。这避免把预计重置时间当成已经恢复。

首次尝试之后，未恢复、会话仍忙或再次受限时每小时再尝试一次，最多重试七次。也就是一轮最多八次恢复尝试；第七次重试仍未完成时，进程写入日志并自行结束。遇到提交结果不明、人工输入或非额度错误时，自动发送保持暂停，避免重复提交。

## 启动与停止

从仓库根目录启动，传入当前 main 会话 ID：

```sh
python3 bin/start-codex-quota-watch.py <MAIN_THREAD_ID>
```

如果桌面 App 已提供可验证的 app-server 控制 socket，可同时传入：

```sh
python3 bin/start-codex-quota-watch.py <MAIN_THREAD_ID> \
  --server-socket /path/to/app-server-control.sock
```

停止时运行：

```sh
python3 bin/stop-codex-quota-watch.py
```

停止脚本只创建停止标记。监控处于等待时约一秒内退出；如果正在等待一轮“继续”的结果，会让该轮结束后退出。

日志在 `runtime/codex-quota-watch/monitor.jsonl`。启动脚本会阻止同一运行目录中的重复进程；已退出的旧 PID 会在下次启动时清理。

## 限制

独立启动的 app-server 可以读取额度，但不能自动证明它拥有桌面 main 会话的完整工具环境。要保留桌面会话的工具，需要已验证的标准控制 socket；内部 IPC socket 不能用于此目的。
