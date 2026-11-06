#!/usr/bin/env python3
"""Deterministic full-test runner for yolo-agents.

The runner never repairs code. It executes every independent check, writes one
redacted JSON summary plus per-check logs, and exits non-zero when a required
check fails. Live installation is added as a separate phase by product tooling.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("automated", "upgrade"))
    parser.add_argument("--product", choices=("yolo-trade",), required=True)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--baseline-macos-app", type=Path)
    parser.add_argument("--candidate-macos-app", type=Path)
    parser.add_argument("--baseline-ios-app", type=Path)
    parser.add_argument("--candidate-ios-app", type=Path)
    args = parser.parse_args(argv)
    report_dir = create_report_dir(args.report_dir)
    if args.phase == "automated":
        report, code = execute_automated(args.product, report_dir, dry_run=args.dry_run)
    else:
        report, code = execute_upgrade(
            args.product,
            report_dir,
            baseline_macos=args.baseline_macos_app,
            candidate_macos=args.candidate_macos_app,
            baseline_ios=args.baseline_ios_app,
            candidate_ios=args.candidate_ios_app,
            dry_run=args.dry_run,
        )
    print(json.dumps({"status": report["status"], "report": str(report_dir / "report.json")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
