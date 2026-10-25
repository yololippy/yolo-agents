#!/usr/bin/env python3
"""Request graceful shutdown of the repository-owned Codex quota monitor."""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime" / "codex-quota-watch"


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    stop_file = RUNTIME / "stop"
    stop_file.touch()
    pid_file = RUNTIME / "monitor.pid"
    detail = pid_file.read_text().strip() if pid_file.exists() else "unknown"
    print(f"Requested graceful shutdown for quota monitor (PID {detail}).")
    print(f"Stop marker: {stop_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
