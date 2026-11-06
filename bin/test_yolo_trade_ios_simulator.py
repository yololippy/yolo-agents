#!/usr/bin/env python3
"""Run the hosted YoloTrade SDK tests in an isolated iOS Simulator."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents/yolo-trade/runtime-python"))

from yolo_trade.apple_upgrade import DEFAULT_DEVELOPER_DIR  # noqa: E402
from yolo_trade.upgrade_test import UpgradeTestError, select_simulator  # noqa: E402


class SimulatorSDKTestError(RuntimeError):
    pass


def _tool(name: str) -> str:
    developer = Path(os.environ.get("DEVELOPER_DIR", str(DEFAULT_DEVELOPER_DIR)))
    return str(developer / "usr/bin" / name)


def _capture(command: list[str], *, run=subprocess.run, message: str) -> bytes:
    try:
        result = run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError:
        raise SimulatorSDKTestError(message) from None
    if result.returncode != 0:
        raise SimulatorSDKTestError(message)
    return result.stdout


def run_hosted_sdk_tests(
    derived_data: Path,
    *,
    root: Path = ROOT,
    run=subprocess.run,
    selector=select_simulator,
) -> dict[str, object]:
    try:
        runtime, device_type = selector(run=run)
    except UpgradeTestError as error:
        raise SimulatorSDKTestError(str(error)) from None
    name = f"YoloTrade SDK Full Test {uuid4().hex[:8]}"
    device = _capture(
        [_tool("simctl"), "create", name, device_type, runtime],
        run=run,
        message="无法创建隔离 iOS Simulator。",
    ).decode().strip()
    if not device:
        raise SimulatorSDKTestError("创建 Simulator 后没有获得设备标识。")
    try:
        _capture(
            [_tool("simctl"), "boot", device], run=run,
            message="无法启动隔离 iOS Simulator。",
        )
        _capture(
            [_tool("simctl"), "bootstatus", device, "-b"], run=run,
            message="隔离 iOS Simulator 未完成启动。",
        )
        command = [
            _tool("xcodebuild"),
            "-project", "apps/yolo-trade/xcode/YoloTrade.xcodeproj",
            "-scheme", "YoloTradeIOS",
            "-configuration", "Debug",
            "-destination", f"platform=iOS Simulator,id={device}",
            "-derivedDataPath", str(derived_data.resolve()),
            "-skipMacroValidation",
            "ARCHS=arm64",
            "ONLY_ACTIVE_ARCH=YES",
            "SWIFT_ENABLE_EXPLICIT_MODULES=NO",
            "-parallel-testing-enabled", "NO",
            "-maximum-concurrent-test-simulator-destinations", "1",
            "-quiet",
            "test",
        ]
        try:
            result = run(command, cwd=root, check=False)
        except OSError:
            raise SimulatorSDKTestError("无法启动 Simulator SDK 测试。") from None
        if result.returncode != 0:
            raise SimulatorSDKTestError("Simulator SDK 测试未通过。")
        return {
            "status": "passed",
            "hosted_test_target": "YoloTradeIOSSDKTests",
            "architecture": "arm64",
            "device_identifier_recorded": False,
        }
    finally:
        run(
            [_tool("simctl"), "shutdown", device],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        run(
            [_tool("simctl"), "delete", device],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derived-data", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_hosted_sdk_tests(args.derived_data)
    except SimulatorSDKTestError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
