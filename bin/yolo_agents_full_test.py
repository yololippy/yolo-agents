#!/usr/bin/env python3
"""Deterministic full-test runner for yolo-agents.

The runner never repairs code. It executes every independent check, writes one
redacted JSON summary plus per-check logs, and exits non-zero when a required
check fails. Automated, isolated upgrade, live installation, and acceptance are
separate explicit phases.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_ROOT = ROOT / ".agents" / "runtime" / "yolo-agents-full-test"
sys.path.insert(0, str(ROOT / "packages/core-python"))
sys.path.insert(0, str(ROOT / "agents/yolo-trade/runtime-python"))

from yolo_trade.upgrade_test import (  # noqa: E402
    UpgradeTestError,
    exercise_ios_simulator_upgrade,
    exercise_macos_upgrade,
    tree_fingerprint,
)
from yolo_trade.apple_upgrade import (  # noqa: E402
    replace_ios_app,
    replace_macos_app,
    restore_macos_app,
    validate_ios_bundle,
    validate_macos_bundle,
)
from yolo_trade.full_test import FullTestPreflightError, live_preflight  # noqa: E402
from yolo_trade.host_service import _paths as host_service_paths, install as install_host_service  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 1800


@dataclass
class CheckResult:
    name: str
    status: str
    exit_code: int | None
    duration_seconds: float
    log: str
    command: list[str]


@dataclass
class UpgradeCheckResult:
    name: str
    status: str
    duration_seconds: float
    log: str
    baseline: str | None
    candidate: str | None
    baseline_fingerprint: str | None
    candidate_fingerprint: str | None
    details: dict[str, object]


class FullTestAlreadyRunning(RuntimeError):
    pass


@contextmanager
def full_test_lock(product: str):
    DEFAULT_REPORT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = DEFAULT_REPORT_ROOT / f"{product}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FullTestAlreadyRunning("同一产品已有完整测试阶段正在运行。") from None
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def python_executable() -> str:
    candidate = ROOT / ".venv" / "bin" / "python"
    return str(candidate if candidate.is_file() else Path(sys.executable))


def automated_checks(product: str, report_dir: Path) -> list[Check]:
    if product != "yolo-trade":
        raise ValueError(f"unsupported full-test product: {product}")
    python = python_executable()
    swift = "/usr/bin/xcrun"
    xcodebuild = "/usr/bin/xcodebuild"
    project = "apps/yolo-trade/xcode/YoloTrade.xcodeproj"
    return [
        Check("full-test-runner", (python, "-m", "unittest", "discover", "-s", "bin/tests", "-p", "test_*.py")),
        Check("python-core", (python, "-m", "unittest", "discover", "-s", "packages/core-python/tests")),
        Check("python-yolo-trade", (python, "-m", "unittest", "discover", "-s", "agents/yolo-trade/runtime-python/tests")),
        Check("agent-isolation", (python, "bin/test-agent-isolation.py")),
        Check("swift-sdk", (swift, "swift", "test", "--package-path", "packages/sdk-swift")),
        Check("swift-ui", (swift, "swift", "test", "--package-path", "packages/ui-swift")),
        Check("swift-yolo-trade", (swift, "swift", "test", "--package-path", "apps/yolo-trade/client-swift")),
        Check("swift-python-integration", (python, "bin/test-chat-integration.py", "--sdk-only")),
        Check("xcode-macos", (
            xcodebuild, "-project", project, "-scheme", "YoloTradeMac", "-configuration", "Debug",
            "-destination", "platform=macOS,arch=arm64", "-derivedDataPath", str(report_dir / "derived-macos"),
            "-skipMacroValidation", "ARCHS=arm64", "ONLY_ACTIVE_ARCH=YES", "SWIFT_ENABLE_EXPLICIT_MODULES=NO", "-quiet", "build",
        )),
        Check("xcode-ios-simulator", (
            xcodebuild, "-project", project, "-scheme", "YoloTradeIOS", "-configuration", "Debug",
            "-destination", "generic/platform=iOS Simulator", "-derivedDataPath", str(report_dir / "derived-ios"),
            "-skipMacroValidation", "SWIFT_ENABLE_EXPLICIT_MODULES=NO", "-quiet", "build",
        )),
        Check("xcode-ios-simulator-tests", (
            python, "bin/test_yolo_trade_ios_simulator.py",
            "--derived-data", str(report_dir / "derived-ios-tests"),
        )),
        Check("xcode-ios-device", (
            xcodebuild, "-project", project, "-scheme", "YoloTradeIOS", "-configuration", "Debug",
            "-destination", "generic/platform=iOS", "-derivedDataPath", str(report_dir / "derived-ios-device"),
            "-skipMacroValidation", "SWIFT_ENABLE_EXPLICIT_MODULES=NO", "-quiet", "build",
        )),
    ]


def candidate_state(run=subprocess.run) -> dict[str, object]:
    def git(*arguments: str) -> bytes:
        result = run(["git", *arguments], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return result.stdout

    revision = git("rev-parse", "HEAD").decode().strip()
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    tracked_diff = git("diff", "--binary", "HEAD")
    untracked = [item for item in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if item]
    digest = hashlib.sha256(status + b"\0" + tracked_diff)
    for encoded in sorted(untracked):
        path = ROOT / os.fsdecode(encoded)
        digest.update(b"\0untracked\0" + encoded + b"\0")
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    fingerprint = digest.hexdigest()
    return {"revision": revision, "dirty": bool(status.strip()), "difference_fingerprint": fingerprint}


def safe_environment() -> dict[str, str]:
    environment = dict(os.environ)
    paths = [ROOT / "packages/core-python", ROOT / "agents/yolo-trade/runtime-python", ROOT / "agents/yolo-beni/runtime-python"]
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join([*(str(path) for path in paths), *([existing] if existing else [])])
    environment["DEVELOPER_DIR"] = environment.get("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")
    return environment


def run_check(check: Check, report_dir: Path, *, run=subprocess.run, dry_run: bool = False) -> CheckResult:
    log_path = report_dir / "logs" / f"{check.name}.log"
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if dry_run:
        log_path.write_text("dry run\n", encoding="utf-8")
        log_path.chmod(0o600)
        return CheckResult(check.name, "not_run", None, 0.0, str(log_path.relative_to(report_dir)), list(check.command))
    started = time.monotonic()
    try:
        with log_path.open("wb") as log:
            result = run(
                list(check.command), cwd=ROOT, env=safe_environment(), stdout=log, stderr=subprocess.STDOUT,
                timeout=check.timeout_seconds, check=False,
            )
        status = "passed" if result.returncode == 0 else "failed"
        code = result.returncode
    except subprocess.TimeoutExpired:
        with log_path.open("ab") as log:
            log.write(b"\nfull-test check timed out\n")
        status, code = "failed", None
    except OSError as error:
        with log_path.open("ab") as log:
            log.write(f"\nfull-test could not start the check: {type(error).__name__}\n".encode())
        status, code = "failed", None
    duration = round(time.monotonic() - started, 3)
    log_path.chmod(0o600)
    return CheckResult(check.name, status, code, duration, str(log_path.relative_to(report_dir)), list(check.command))


def create_report_dir(requested: Path | None) -> Path:
    if requested is not None:
        requested.mkdir(mode=0o700, parents=True, exist_ok=False)
        return requested.resolve()
    DEFAULT_REPORT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path(tempfile.mkdtemp(prefix=f"{stamp}-", dir=DEFAULT_REPORT_ROOT)).resolve()


def automated_artifacts(report_dir: Path) -> dict[str, dict[str, str]]:
    expected = {
        "macos": report_dir / "derived-macos/Build/Products/Debug/YoloTradeMac.app",
        "ios_simulator": report_dir / "derived-ios/Build/Products/Debug-iphonesimulator/YoloTradeIOS.app",
        "ios_device": report_dir / "derived-ios-device/Build/Products/Debug-iphoneos/YoloTradeIOS.app",
    }
    return {
        name: {
            "path": str(path.relative_to(report_dir)),
            "fingerprint": tree_fingerprint(path),
        }
        for name, path in expected.items() if path.is_dir()
    }


def execute_automated(product: str, report_dir: Path, *, dry_run: bool = False, run=subprocess.run) -> tuple[dict, int]:
    checks = automated_checks(product, report_dir)
    results = [run_check(check, report_dir, run=run, dry_run=dry_run) for check in checks]
    report = {
        "schema_version": 1,
        "product": product,
        "phase": "automated",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate": candidate_state() if not dry_run else {"revision": "dry-run", "dirty": None, "difference_fingerprint": None},
        "status": "not_run" if dry_run else ("passed" if all(item.status == "passed" for item in results) else "failed"),
        "checks": [asdict(item) for item in results],
        "artifacts": {} if dry_run else automated_artifacts(report_dir),
    }
    destination = report_dir / "report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return report, 0 if report["status"] in ("passed", "not_run") else 1


def run_upgrade_check(name: str, baseline: Path | None, candidate: Path | None, workspace: Path,
                      exercise, *, dry_run: bool = False) -> UpgradeCheckResult:
    report_dir = workspace.parents[1]
    log_path = report_dir / "logs" / f"{name}.log"
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = time.monotonic()
    baseline_value = str(baseline.resolve()) if baseline is not None else None
    candidate_value = str(candidate.resolve()) if candidate is not None else None
    baseline_fingerprint = None
    candidate_fingerprint = None
    details: dict[str, object] = {}
    if dry_run:
        status = "not_run"
        message = "dry run"
    elif baseline is None:
        status = "blocked"
        message = "没有明确提供可靠的旧版基线产物。"
    elif candidate is None:
        status = "blocked"
        message = "没有明确提供候选产物。"
    elif not baseline.is_dir():
        status = "blocked"
        message = "旧版基线产物不存在或不是 App 目录。"
    elif not candidate.is_dir():
        status = "blocked"
        message = "候选产物不存在或不是 App 目录。"
    else:
        try:
            baseline_fingerprint = tree_fingerprint(baseline)
            candidate_fingerprint = tree_fingerprint(candidate)
            if baseline_fingerprint == candidate_fingerprint:
                status = "blocked"
                message = "旧版基线与候选产物完全相同，不能证明发生过升级。"
            else:
                workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
                details = exercise(baseline, candidate, workspace)
                status = "passed"
                message = json.dumps(details, ensure_ascii=False, sort_keys=True)
        except Exception as error:
            status = "failed"
            message = f"{type(error).__name__}: {error}"
        finally:
            try:
                if workspace.exists():
                    shutil.rmtree(workspace)
            except OSError as error:
                status = "failed"
                message += f"\n清理隔离升级目录失败：{type(error).__name__}"
    log_path.write_text(message + "\n", encoding="utf-8")
    log_path.chmod(0o600)
    return UpgradeCheckResult(
        name=name,
        status=status,
        duration_seconds=round(time.monotonic() - started, 3),
        log=str(log_path.relative_to(report_dir)),
        baseline=baseline_value,
        candidate=candidate_value,
        baseline_fingerprint=baseline_fingerprint,
        candidate_fingerprint=candidate_fingerprint,
        details=details,
    )


def execute_upgrade(product: str, report_dir: Path, *, baseline_macos: Path | None,
                    candidate_macos: Path | None, baseline_ios: Path | None,
                    candidate_ios: Path | None, dry_run: bool = False,
                    mac_exercise=exercise_macos_upgrade,
                    ios_exercise=exercise_ios_simulator_upgrade) -> tuple[dict, int]:
    if product != "yolo-trade":
        raise ValueError(f"unsupported full-test product: {product}")
    workspaces = report_dir / "workspaces"
    results = [
        run_upgrade_check(
            "macos-upgrade", baseline_macos, candidate_macos, workspaces / "macos",
            mac_exercise, dry_run=dry_run,
        ),
        run_upgrade_check(
            "ios-simulator-upgrade", baseline_ios, candidate_ios, workspaces / "ios",
            ios_exercise, dry_run=dry_run,
        ),
    ]
    statuses = {item.status for item in results}
    if statuses == {"not_run"}:
        status = "not_run"
    elif "failed" in statuses:
        status = "failed"
    elif "blocked" in statuses:
        status = "blocked"
    else:
        status = "passed"
    report = {
        "schema_version": 1,
        "product": product,
        "phase": "upgrade",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate": candidate_state() if not dry_run else {
            "revision": "dry-run", "dirty": None, "difference_fingerprint": None,
        },
        "status": status,
        "checks": [asdict(item) for item in results],
    }
    destination = report_dir / "report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return report, 0 if status in ("passed", "not_run") else 1


class LivePhaseBlocked(RuntimeError):
    pass


def _passing_phase_report(path: Path | None, phase: str) -> dict:
    if path is None or not path.is_file():
        raise LivePhaseBlocked(f"缺少已完成的 {phase} 阶段报告。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LivePhaseBlocked(f"{phase} 阶段报告不可读取。") from None
    if not isinstance(value, dict) or value.get("phase") != phase or value.get("status") != "passed":
        raise LivePhaseBlocked(f"{phase} 阶段尚未通过。")
    return value


def _matching_candidate(report: dict, current: dict, phase: str) -> None:
    if report.get("candidate") != current:
        raise LivePhaseBlocked(f"{phase} 阶段验证的不是当前候选代码状态。")


def wait_for_live_preflight(preflight, *, device: str, mac_destination: Path,
                            timeout_seconds: float = 120, sleep=time.sleep) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return preflight(device=device, mac_destination=mac_destination)
        except FullTestPreflightError:
            if time.monotonic() >= deadline:
                raise FullTestPreflightError("Host 重启后未在时限内恢复完整就绪状态。") from None
            sleep(1)


def execute_live(
    product: str,
    report_dir: Path,
    *,
    automated_report: Path | None,
    upgrade_report: Path | None,
    mac_candidate: Path | None,
    mac_destination: Path | None,
    mac_backup_root: Path | None,
    ios_candidate: Path | None,
    ios_rollback: Path | None,
    device: str | None,
    dry_run: bool = False,
    state_reader=candidate_state,
    preflight=live_preflight,
    mac_installer=replace_macos_app,
    mac_restorer=restore_macos_app,
    ios_installer=replace_ios_app,
    mac_validator=validate_macos_bundle,
    ios_validator=validate_ios_bundle,
    host_installer=install_host_service,
    host_config_reader=lambda: host_service_paths()["config"],
    preflight_waiter=wait_for_live_preflight,
) -> tuple[dict, int]:
    if product != "yolo-trade":
        raise ValueError(f"unsupported full-test product: {product}")
    started = time.monotonic()
    log_path = report_dir / "logs/live.log"
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = {"revision": "dry-run", "dirty": None, "difference_fingerprint": None} if dry_run else state_reader()
    status = "not_run" if dry_run else "blocked"
    details: dict[str, object] = {}
    message = "dry run" if dry_run else ""
    mac_result = None
    ios_attempted = False
    if not dry_run:
        try:
            required = {
                "automated_report": automated_report,
                "upgrade_report": upgrade_report,
                "mac_candidate": mac_candidate,
                "mac_destination": mac_destination,
                "mac_backup_root": mac_backup_root,
                "ios_candidate": ios_candidate,
                "ios_rollback": ios_rollback,
            }
            missing = [name for name, value in required.items() if value is None]
            if device is None:
                missing.append("device")
            if missing:
                raise LivePhaseBlocked("live 阶段缺少显式输入：" + ", ".join(missing))
            automated = _passing_phase_report(automated_report, "automated")
            upgrade = _passing_phase_report(upgrade_report, "upgrade")
            _matching_candidate(automated, current, "automated")
            _matching_candidate(upgrade, current, "upgrade")
            automated_checks = {item.get("name"): item.get("status") for item in automated.get("checks", [])}
            if automated_checks.get("xcode-macos") != "passed" or automated_checks.get("xcode-ios-device") != "passed":
                raise LivePhaseBlocked("automated 阶段缺少通过的 Mac 或 iPhone 真机构建。")
            upgrade_checks = {item.get("name"): item for item in upgrade.get("checks", [])}
            if (upgrade_checks.get("macos-upgrade", {}).get("status") != "passed"
                    or upgrade_checks.get("ios-simulator-upgrade", {}).get("status") != "passed"):
                raise LivePhaseBlocked("upgrade 阶段缺少通过的 Mac 或 iOS 隔离升级。")
            mac_fingerprint = tree_fingerprint(mac_candidate)
            automated_artifact_values = automated.get("artifacts") if isinstance(automated.get("artifacts"), dict) else {}
            if automated_artifact_values.get("macos", {}).get("fingerprint") != mac_fingerprint:
                raise LivePhaseBlocked("live Mac 候选产物不是 automated 阶段构建的产物。")
            if upgrade_checks["macos-upgrade"].get("candidate_fingerprint") != mac_fingerprint:
                raise LivePhaseBlocked("live Mac 候选产物不是 upgrade 阶段验证过的产物。")
            ios_fingerprint = tree_fingerprint(ios_candidate)
            rollback_fingerprint = tree_fingerprint(ios_rollback)
            if automated_artifact_values.get("ios_device", {}).get("fingerprint") != ios_fingerprint:
                raise LivePhaseBlocked("live iPhone 候选产物不是 automated 阶段构建的真机产物。")
            if ios_fingerprint == rollback_fingerprint:
                raise LivePhaseBlocked("iPhone 候选与回滚产物相同，不能证明升级或恢复。")
            mac_validator(mac_candidate)
            ios_validator(ios_candidate)
            ios_validator(ios_rollback)
            before = preflight(device=device, mac_destination=mac_destination)
            host_installer(host_config_reader())
            after_host_restart = preflight_waiter(
                preflight, device=device, mac_destination=mac_destination,
            )
            mac_result = mac_installer(mac_candidate, mac_destination, mac_backup_root)
            ios_attempted = True
            ios_result = ios_installer(
                ios_candidate, device, rollback_candidate=ios_rollback,
            )
            after = preflight(device=device, mac_destination=mac_destination)
            details = {
                "preflight": before,
                "post_host_restart": after_host_restart,
                "post_install": after,
                "host_restarted": True,
                "macos": {
                    "candidate_fingerprint": mac_fingerprint,
                    "backup_created": mac_result.backup is not None,
                    "short_version": mac_result.metadata.short_version,
                    "build_version": mac_result.metadata.build_version,
                },
                "ios": {
                    "candidate_fingerprint": ios_fingerprint,
                    "rollback_fingerprint": rollback_fingerprint,
                    "short_version": ios_result.metadata.short_version,
                    "build_version": ios_result.metadata.build_version,
                },
                "device_identifier_recorded": False,
            }
            status = "passed"
            message = json.dumps(details, ensure_ascii=False, sort_keys=True)
        except LivePhaseBlocked as error:
            status = "blocked"
            message = str(error)
        except Exception as error:
            status = "failed"
            recovery = []
            if ios_attempted and ios_rollback is not None and device is not None:
                try:
                    ios_installer(ios_rollback, device, require_existing=True)
                    recovery.append("ios_restored")
                except Exception as rollback_error:
                    recovery.append(f"ios_restore_failed:{type(rollback_error).__name__}")
            if mac_result is not None and mac_result.backup is not None and mac_destination is not None and mac_backup_root is not None:
                try:
                    mac_restorer(Path(mac_result.backup), mac_destination, mac_backup_root)
                    recovery.append("macos_restored")
                except Exception as rollback_error:
                    recovery.append(f"macos_restore_failed:{type(rollback_error).__name__}")
            details = {"recovery": recovery, "device_identifier_recorded": False}
            message = f"{type(error).__name__}: {error}\n" + json.dumps(details, ensure_ascii=False, sort_keys=True)
    log_path.write_text(message + "\n", encoding="utf-8")
    log_path.chmod(0o600)
    report = {
        "schema_version": 1,
        "product": product,
        "phase": "live",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate": current,
        "status": status,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": str(log_path.relative_to(report_dir)),
        "details": details,
    }
    destination = report_dir / "report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return report, 0 if status in ("passed", "not_run") else 1


def run_stored_model_probe(*, run=subprocess.run) -> dict[str, object]:
    command = [
        "/usr/bin/xcrun", "swift", "run", "--quiet", "--package-path",
        str(ROOT / "apps/yolo-trade/client-swift"), "YoloTradeAcceptanceProbe",
    ]
    try:
        result = run(
            command,
            cwd=ROOT,
            env=safe_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=75,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Mac 模型验收探针未能启动或超时。") from None
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise RuntimeError("Mac 模型验收探针未通过。")
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("Mac 模型验收探针返回格式无效。") from None
    expected = {
        "status": "passed",
        "agent_id": "yolo-trade",
        "provider_authenticated": True,
        "model_catalog_available": True,
        "mac_client_online": True,
        "model_roundtrip_passed": True,
        "business_tools_enabled": False,
        "conversation_saved": False,
    }
    if not isinstance(value, dict) or any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise RuntimeError("Mac 模型验收探针没有满足全部门槛。")
    if not isinstance(value.get("model"), str) or not value["model"]:
        raise RuntimeError("Mac 模型验收探针缺少模型标识。")
    return {key: value[key] for key in (*expected, "model")}


def execute_acceptance(
    product: str,
    report_dir: Path,
    *,
    live_report: Path | None,
    mac_destination: Path | None,
    device: str | None,
    ios_client_online: bool,
    dry_run: bool = False,
    state_reader=candidate_state,
    preflight=live_preflight,
    model_probe=run_stored_model_probe,
) -> tuple[dict, int]:
    if product != "yolo-trade":
        raise ValueError(f"unsupported full-test product: {product}")
    started = time.monotonic()
    log_path = report_dir / "logs/acceptance.log"
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = {"revision": "dry-run", "dirty": None, "difference_fingerprint": None} if dry_run else state_reader()
    details: dict[str, object] = {"device_identifier_recorded": False}
    if dry_run:
        status, message = "not_run", "dry run"
    else:
        try:
            if live_report is None or mac_destination is None or device is None:
                raise LivePhaseBlocked("acceptance 阶段缺少 live 报告、Mac 安装位置或本轮设备。")
            live = _passing_phase_report(live_report, "live")
            _matching_candidate(live, current, "live")
        except LivePhaseBlocked as error:
            status, message = "blocked", str(error)
        else:
            checks = []
            try:
                snapshot = preflight(device=device, mac_destination=mac_destination)
                checks.append({"name": "product-state", "status": "passed", "details": snapshot})
            except Exception as error:
                checks.append({
                    "name": "product-state", "status": "failed",
                    "error": type(error).__name__,
                })
            try:
                probe = model_probe()
                checks.append({"name": "mac-provider-model", "status": "passed", "details": probe})
            except Exception as error:
                checks.append({
                    "name": "mac-provider-model", "status": "failed",
                    "error": type(error).__name__,
                })
            checks.append({
                "name": "ios-client-online-sync",
                "status": "passed" if ios_client_online else "blocked",
                "details": {"verified_in_current_ui_session": ios_client_online},
            })
            details.update({"checks": checks})
            statuses = {item["status"] for item in checks}
            status = "failed" if "failed" in statuses else "blocked" if "blocked" in statuses else "passed"
            message = json.dumps(details, ensure_ascii=False, sort_keys=True)
    log_path.write_text(message + "\n", encoding="utf-8")
    log_path.chmod(0o600)
    report = {
        "schema_version": 1,
        "product": product,
        "phase": "acceptance",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate": current,
        "status": status,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": str(log_path.relative_to(report_dir)),
        "details": details,
    }
    destination = report_dir / "report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return report, 0 if status in ("passed", "not_run") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("automated", "upgrade", "live", "acceptance"))
    parser.add_argument("--product", choices=("yolo-trade",), required=True)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--baseline-macos-app", type=Path)
    parser.add_argument("--candidate-macos-app", type=Path)
    parser.add_argument("--baseline-ios-app", type=Path)
    parser.add_argument("--candidate-ios-app", type=Path)
    parser.add_argument("--automated-report", type=Path)
    parser.add_argument("--upgrade-report", type=Path)
    parser.add_argument("--mac-candidate-app", type=Path)
    parser.add_argument("--mac-destination", type=Path)
    parser.add_argument("--mac-backup-root", type=Path)
    parser.add_argument("--ios-candidate-app", type=Path)
    parser.add_argument("--ios-rollback-app", type=Path)
    parser.add_argument("--device")
    parser.add_argument("--live-report", type=Path)
    parser.add_argument("--ios-client-online", action="store_true")
    args = parser.parse_args(argv)
    report_dir = create_report_dir(args.report_dir)
    try:
        with full_test_lock(args.product):
            if args.phase == "automated":
                report, code = execute_automated(args.product, report_dir, dry_run=args.dry_run)
            elif args.phase == "upgrade":
                report, code = execute_upgrade(
                    args.product,
                    report_dir,
                    baseline_macos=args.baseline_macos_app,
                    candidate_macos=args.candidate_macos_app,
                    baseline_ios=args.baseline_ios_app,
                    candidate_ios=args.candidate_ios_app,
                    dry_run=args.dry_run,
                )
            elif args.phase == "live":
                report, code = execute_live(
                    args.product,
                    report_dir,
                    automated_report=args.automated_report,
                    upgrade_report=args.upgrade_report,
                    mac_candidate=args.mac_candidate_app,
                    mac_destination=args.mac_destination,
                    mac_backup_root=args.mac_backup_root,
                    ios_candidate=args.ios_candidate_app,
                    ios_rollback=args.ios_rollback_app,
                    device=args.device,
                    dry_run=args.dry_run,
                )
            else:
                report, code = execute_acceptance(
                    args.product,
                    report_dir,
                    live_report=args.live_report,
                    mac_destination=args.mac_destination,
                    device=args.device,
                    ios_client_online=args.ios_client_online,
                    dry_run=args.dry_run,
                )
    except FullTestAlreadyRunning as error:
        report, code = {
            "schema_version": 1,
            "product": args.product,
            "phase": args.phase,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "blocked",
            "reason": str(error),
        }, 1
        destination = report_dir / "report.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        destination.chmod(0o600)
    print(json.dumps({"status": report["status"], "report": str(report_dir / "report.json")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
