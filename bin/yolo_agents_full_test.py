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
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_ROOT = ROOT / ".agents" / "runtime" / "yolo-agents-full-test"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("automated",))
    parser.add_argument("--product", choices=("yolo-trade",), required=True)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    report_dir = create_report_dir(args.report_dir)
    report, code = execute_automated(args.product, report_dir, dry_run=args.dry_run)
    print(json.dumps({"status": report["status"], "report": str(report_dir / "report.json")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
