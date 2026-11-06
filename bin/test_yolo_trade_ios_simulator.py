#!/usr/bin/env python3
"""Run the hosted YoloTrade SDK tests in an isolated iOS Simulator."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
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


def _issue_production_session(
    *, root: Path, host_endpoint: str, mac_app: Path, run=subprocess.run,
) -> dict[str, object]:
    output = run_signed_acceptance_probe(
        ["--simulator-session", "--endpoint", host_endpoint],
        root=root, mac_app=mac_app, run=run,
    )
    try:
        session = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise SimulatorSDKTestError("Simulator 真实模型验收会话格式无效。") from None
    expected = {
        "endpoint", "pairing_code", "grant", "expires_in",
        "allow_insecure_local_http",
    }
    if (not isinstance(session, dict) or set(session) != expected
            or not all(isinstance(session[key], str) and session[key]
                       for key in {"endpoint", "pairing_code", "grant"})
            or not isinstance(session["expires_in"], int) or session["expires_in"] <= 0
            or not isinstance(session["allow_insecure_local_http"], bool)):
        raise SimulatorSDKTestError("Simulator 真实模型验收会话格式无效。")
    return session


def run_signed_acceptance_probe(
    arguments: list[str], *, root: Path, mac_app: Path,
    run=subprocess.run, timeout: int = 75,
) -> bytes:
    package = root / "apps/yolo-trade/client-swift"
    environment = dict(os.environ)
    environment["DEVELOPER_DIR"] = environment.get(
        "DEVELOPER_DIR", str(DEFAULT_DEVELOPER_DIR),
    )
    try:
        build = run(
            ["/usr/bin/xcrun", "swift", "build", "--quiet", "--package-path", str(package),
             "--product", "YoloTradeAcceptanceProbe"],
            cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SimulatorSDKTestError("无法构建已签名的 YoloTrade 验收探针。") from None
    if build.returncode != 0:
        raise SimulatorSDKTestError("无法构建已签名的 YoloTrade 验收探针。")
    try:
        bin_path = run(
            ["/usr/bin/xcrun", "swift", "build", "--show-bin-path", "--package-path", str(package)],
            cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
        signature = run(
            ["/usr/bin/codesign", "-d", "--verbose=2", str(mac_app)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
        identities = run(
            ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SimulatorSDKTestError("无法核对 YoloTrade Mac 签名身份。") from None
    if bin_path.returncode != 0 or signature.returncode != 0 or identities.returncode != 0:
        raise SimulatorSDKTestError("无法核对 YoloTrade Mac 签名身份。")
    executable = Path(bin_path.stdout.decode("utf-8").strip()) / "YoloTradeAcceptanceProbe"
    metadata = signature.stderr.decode("utf-8", errors="replace")
    identifier = re.search(r"^Identifier=(.+)$", metadata, re.MULTILINE)
    authority = re.search(r"^Authority=(.+)$", metadata, re.MULTILINE)
    if (identifier is None or authority is None
            or identifier.group(1) != "com.yoloagents.yolotrade" or not executable.is_file()):
        raise SimulatorSDKTestError("YoloTrade Mac 签名身份与验收探针不兼容。")
    available = identities.stdout.decode("utf-8", errors="replace")
    identity = next((match.group(1) for match in re.finditer(
        r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"]+)"', available, re.MULTILINE,
    ) if match.group(2) == authority.group(1)), None)
    if identity is None:
        raise SimulatorSDKTestError("找不到与 YoloTrade Mac 相同的代码签名身份。")
    try:
        signed = run(
            ["/usr/bin/codesign", "--force", "--sign", identity,
             "--identifier", identifier.group(1), str(executable)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SimulatorSDKTestError("无法为 YoloTrade 验收探针应用 Mac 身份签名。") from None
    if signed.returncode != 0:
        raise SimulatorSDKTestError("无法为 YoloTrade 验收探针应用 Mac 身份签名。")
    try:
        result = run(
            [str(executable), *arguments], cwd=root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SimulatorSDKTestError("已签名的 YoloTrade 验收探针未能启动或超时。") from None
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise SimulatorSDKTestError("已签名的 YoloTrade 验收探针未通过。")
    return result.stdout


def _configured_xctestrun(source: Path, destination: Path, environment: dict[str, str]) -> None:
    original_root = str(source.resolve().parent)
    configuration = plistlib.loads(source.read_bytes())
    targets = 0

    def configure(value):
        nonlocal targets
        if isinstance(value, str):
            return value.replace("__TESTROOT__", original_root)
        if isinstance(value, list):
            return [configure(item) for item in value]
        if isinstance(value, dict):
            result = {key: configure(item) for key, item in value.items()}
            if "TestBundlePath" in result:
                result.setdefault("EnvironmentVariables", {}).update(environment)
                targets += 1
            return result
        return value

    configured = configure(configuration)
    if targets == 0:
        raise SimulatorSDKTestError("构建产物没有可配置的 Simulator 测试目标。")
    destination.write_bytes(plistlib.dumps(configured))


def run_hosted_sdk_tests(
    derived_data: Path,
    *,
    root: Path = ROOT,
    run=subprocess.run,
    selector=select_simulator,
    production_probe: bool = False,
    host_endpoint: str | None = None,
    mac_app: Path | None = None,
    session_issuer=_issue_production_session,
) -> dict[str, object]:
    if production_probe and (not host_endpoint or mac_app is None):
        raise SimulatorSDKTestError("真实模型验收必须显式提供当前本机 Host 地址和 Mac App。")
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
        common = [
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
        ]
        if production_probe:
            try:
                result = run([*common, "build-for-testing"], cwd=root, check=False)
            except OSError:
                raise SimulatorSDKTestError("无法构建 Simulator SDK 测试。") from None
            if result.returncode != 0:
                raise SimulatorSDKTestError("Simulator SDK 测试构建未通过。")
            configurations = list((derived_data.resolve() / "Build/Products").glob("*.xctestrun"))
            if len(configurations) != 1:
                raise SimulatorSDKTestError("无法唯一确定 Simulator 测试配置。")
            session = session_issuer(
                root=root, host_endpoint=host_endpoint, mac_app=mac_app, run=run,
            )
            injected = {
                "YOLO_TRADE_SIMULATOR_PROBE_ENDPOINT": session["endpoint"],
                "YOLO_TRADE_SIMULATOR_PAIRING_CODE": session["pairing_code"],
                "YOLO_TRADE_SIMULATOR_PROBE_GRANT": session["grant"],
                "YOLO_TRADE_SIMULATOR_ALLOW_INSECURE_LOCAL_HTTP": (
                    "1" if session["allow_insecure_local_http"] else "0"
                ),
            }
            with tempfile.TemporaryDirectory(prefix="yolo-trade-simulator-probe-") as directory:
                configured = Path(directory) / "acceptance.xctestrun"
                _configured_xctestrun(configurations[0], configured, injected)
                try:
                    result = run([
                        _tool("xcodebuild"), "test-without-building",
                        "-xctestrun", str(configured),
                        "-destination", f"platform=iOS Simulator,id={device}",
                        "-parallel-testing-enabled", "NO",
                        "-maximum-concurrent-test-simulator-destinations", "1", "-quiet",
                    ], cwd=root, check=False)
                except OSError:
                    raise SimulatorSDKTestError("无法启动 Simulator SDK 测试。") from None
        else:
            try:
                result = run([*common, "test"], cwd=root, check=False)
            except OSError:
                raise SimulatorSDKTestError("无法启动 Simulator SDK 测试。") from None
        if result.returncode != 0:
            raise SimulatorSDKTestError("Simulator SDK 测试未通过。")
        return {
            "status": "passed",
            "hosted_test_target": "YoloTradeIOSSDKTests",
            "architecture": "arm64",
            "device_identifier_recorded": False,
            "production_model_roundtrip": production_probe,
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
    parser.add_argument(
        "--production-probe", action="store_true",
        help="use a short-lived Mac-issued session for one real Simulator model roundtrip",
    )
    parser.add_argument(
        "--host-endpoint",
        help="explicit local YoloTrade Host endpoint, required with --production-probe",
    )
    parser.add_argument(
        "--mac-app", type=Path,
        help="installed signed YoloTrade Mac app, required with --production-probe",
    )
    args = parser.parse_args(argv)
    try:
        result = run_hosted_sdk_tests(
            args.derived_data, production_probe=args.production_probe,
            host_endpoint=args.host_endpoint, mac_app=args.mac_app,
        )
    except SimulatorSDKTestError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
