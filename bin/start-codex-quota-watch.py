#!/usr/bin/env python3
"""Start the repository-owned Codex quota recovery monitor in the background."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "codex-quota-watch"
WATCHER = ROOT / "bin" / "codex-quota-watch.py"


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread_id", help="main Codex thread to receive the continuation")
    parser.add_argument("--server-socket", help="shared desktop app-server control socket, when available")
    args = parser.parse_args()
    if not args.thread_id.strip():
        parser.error("thread_id cannot be empty")

    RUNTIME.mkdir(parents=True, exist_ok=True)
    pid_file = RUNTIME / "monitor.pid"
    stop_file = RUNTIME / "stop"
    log_file = RUNTIME / "monitor.jsonl"
    if pid_file.exists():
        try:
            previous = int(pid_file.read_text().strip())
        except ValueError:
            previous = 0
        if previous > 0 and running(previous):
            parser.error(f"monitor is already running (PID {previous}); use bin/stop-codex-quota-watch.py")
        pid_file.unlink()
    if stop_file.exists():
        stop_file.unlink()

    command = [
        sys.executable, "-u", str(WATCHER), "--watch",
        "--continue-thread", args.thread_id,
        "--stop-file", str(stop_file),
        "--interval", "3600",
        "--recovery-delay", "3600",
        "--retry-interval", "3600",
        "--max-retries", "7",
    ]
    if args.server_socket:
        command.extend(["--server-socket", args.server_socket])
    with log_file.open("ab", buffering=0) as output:
        child = subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    pid_file.write_text(f"{child.pid}\n")
    print(f"Started quota monitor (PID {child.pid}).")
    print(f"Log: {log_file}")
    print(f"Stop: {ROOT / 'bin' / 'stop-codex-quota-watch.py'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
