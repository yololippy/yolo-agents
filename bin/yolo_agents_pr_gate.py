#!/usr/bin/env python3
"""Frozen PR checks. Only prepare/cleanup mutate Git worktree registrations.

Metadata is a restricted, credential-free export made by main through the
official GitHub connector. This offline tool cannot attest where that export
came from; main must export again immediately before summary and rebase merge.
No command here fetches, pushes, merges, installs apps, or changes services.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("p0", "mac", "simulator")
SCHEMA_VERSION = 1
SHA = re.compile(r"[0-9a-f]{40}\Z")
METADATA_KEYS = frozenset(("repository", "pr_number", "base_ref", "base_sha", "head_ref", "head_sha", "state", "draft"))


class GateError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git(root: Path, *arguments: str, env=None) -> str:
    if env is None:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(["git", *arguments], cwd=root, env=env, capture_output=True, check=False)
    if result.returncode:
        # Git errors can include URLs; do not print subprocess output.
        raise GateError("Git 核对失败：" + arguments[0])
    return result.stdout.decode("utf-8").strip()


def common_dir(root: Path) -> Path:
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def read_json(path: Path) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise GateError("JSON 含重复字段。")
            value[key] = item
        return value
    if path.is_symlink() or not path.is_file():
        raise GateError("缺少常规 JSON 文件。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError):
        raise GateError("JSON 文件无效。") from None
    if not isinstance(value, dict):
        raise GateError("JSON 顶层必须为对象。")
    return value


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise GateError("私有目录不能为符号链接。")
    path.chmod(0o700)


def write_json(path: Path, value: dict, *, exclusive: bool = True) -> None:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(canonical(value) + b"\n")


@contextmanager
def lock(path: Path):
    private_dir(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateError("资源已有检查占用；本次 blocked，不抢占。") from None
        yield
    finally:
        os.close(descriptor)


def metadata(path: Path) -> dict:
    value = read_json(path)
    if set(value) != METADATA_KEYS:
        raise GateError("metadata 必须只包含规定的八个字段。")
    if not isinstance(value["repository"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise GateError("repository 必须为 owner/repo。")
    if type(value["pr_number"]) is not int or value["pr_number"] <= 0:
        raise GateError("无效 PR 编号。")
    if value["state"] != "open" or value["draft"] is not False:
        raise GateError("PR 必须为 open 且非 draft。")
    if value["base_ref"] != "master" or not isinstance(value["head_ref"], str) or not re.fullmatch(r"(?:feat|fix|refactor|test|docs)/[A-Za-z0-9_.\-/]+", value["head_ref"]):
        raise GateError("仅支持 master 上的 feat/fix/refactor/test/docs 分支 PR。")
    if any(not isinstance(value[key], str) or not SHA.fullmatch(value[key]) for key in ("base_sha", "head_sha")):
        raise GateError("head/base 必须为完整 40 位小写提交 SHA。")
    return value


def remote_repository(root: Path) -> tuple[str, str]:
    remote = git(root, "remote", "get-url", "origin")
    match = re.fullmatch(r"git@([A-Za-z0-9.-]+):([^\s:]+?)(?:\.git)?", remote)
    if match is None:
        match = re.fullmatch(r"ssh://git@([A-Za-z0-9.-]+)/([^\s:]+?)(?:\.git)?", remote)
    if match is None or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", match[2]):
        raise GateError("origin 必须为没有凭据的 GitHub SSH 远端。")
    return remote, match[2]


def assert_snapshot(root: Path, head: str) -> None:
    if git(root, "rev-parse", "HEAD") != head:
        raise GateError("工作树 HEAD 与冻结 head 不同。")
    if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise GateError("工作树含未提交或未跟踪改动。")


def assert_refs(root: Path, meta: dict) -> None:
    if git(root, "rev-parse", "refs/remotes/origin/master") != meta["base_sha"]:
        raise GateError("origin/master 已变化或未 fetch 对应 base。")
    if git(root, "rev-parse", f"refs/pull/{meta['pr_number']}/head") != meta["head_sha"]:
        raise GateError("PR ref 已变化或未 fetch refs/pull/N/head。")
    for key in ("base_sha", "head_sha"):
        if git(root, "cat-file", "-t", meta[key]) != "commit":
            raise GateError("snapshot 不是 commit。")


def primary_python(root: Path) -> Path:
    # The first worktree entry is Git's original checkout, never a guessed path.
    listing = git(root, "worktree", "list", "--porcelain")
    primary = listing.splitlines()[0]
    if not primary.startswith("worktree "):
        raise GateError("无法确定主 checkout。")
    candidate = Path(primary.removeprefix("worktree ")) / ".venv/bin/python"
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise GateError("主 checkout 缺少现有 .venv/bin/python；不自动安装依赖。")
    return candidate.absolute()


def signature(value: dict, key: bytes) -> str:
    return hmac.new(key, canonical({k: v for k, v in value.items() if k != "signature"}), hashlib.sha256).hexdigest()


def signed(value: dict, key: bytes) -> dict:
    return dict(value, signature=signature(value, key))


def key_for(run_dir: Path) -> bytes:
    key_path = run_dir.parents[1] / "keys" / f"{run_dir.name}.key"
    if key_path.is_symlink() or not key_path.is_file() or key_path.stat().st_mode & 0o077:
        raise GateError("运行收据密钥无效。")
    key = key_path.read_bytes()
    if len(key) != 32:
        raise GateError("运行收据密钥无效。")
    return key


def verify_signature(value: dict, key: bytes) -> None:
    actual = value.get("signature")
    if not isinstance(actual, str) or not hmac.compare_digest(actual, signature(value, key)):
        raise GateError("运行快照或检查报告收据不匹配。")


def prepare(root: Path, metadata_path: Path, product: str) -> dict:
    if product != "yolo-trade":
        raise GateError("blocked：此产品尚无 PR 检查能力，不能回退到 YoloTrade。")
    root = root.resolve()
    meta = metadata(metadata_path)
    git(root, "check-ref-format", "--branch", meta["head_ref"])
    remote, repository = remote_repository(root)
    if repository.lower() != meta["repository"].lower():
        raise GateError("metadata repository 与 SSH origin 不一致。")
    assert_refs(root, meta)
    assert_snapshot(root, meta["head_sha"])
    python = primary_python(root)
    common = common_dir(root)
    gate_root = common / "yolo-agents-pr-gate"
    with lock(gate_root / "locks/prepare.lock"):
        run_id = uuid.uuid4().hex
        run_dir = gate_root / "runs" / run_id
        private_dir(run_dir)
        private_dir(gate_root / "keys")
        key = os.urandom(32)
        descriptor = os.open(gate_root / "keys" / f"{run_id}.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        manifest = {
            "schema_version": SCHEMA_VERSION, "run_id": run_id, "product": product,
            "created_at": now(), "metadata": meta, "origin": remote,
            "source_worktree": str(root), "common_dir": str(common), "python": str(python),
            "groups": {}, "status": "preparing",
        }
        created = []
        try:
            # These names are never reused or advanced by this tool.
            for label in ("base", "head"):
                git(root, "update-ref", f"refs/yolo-pr-gate/{run_id}/{label}", meta[f"{label}_sha"], "0" * 40)
            private_dir(run_dir / "worktrees")
            for group in GROUPS:
                worktree = run_dir / "worktrees" / group
                private_dir(run_dir / group)
                private_dir(run_dir / group / "build")
                private_dir(run_dir / group / "logs")
                git(root, "worktree", "add", "--detach", str(worktree), meta["head_sha"])
                created.append(worktree)
                assert_snapshot(worktree, meta["head_sha"])
                manifest["groups"][group] = str(worktree)
            # Do not accept a source that changed while preparation ran.
            assert_refs(root, meta)
            assert_snapshot(root, meta["head_sha"])
            manifest["status"] = "prepared"
        except (GateError, OSError):
            manifest["status"] = "blocked"
            for worktree in reversed(created):
                try:
                    git(root, "worktree", "remove", str(worktree))
                except GateError:
                    pass  # Preserve unexpected edits; never force-remove.
            write_json(run_dir / "manifest.json", signed(manifest, key))
            raise GateError(f"prepare 未完成；保留诊断目录：{run_dir}") from None
        write_json(run_dir / "manifest.json", signed(manifest, key))
    return {"status": "prepared", "run_id": run_id, "run_dir": str(run_dir), "head_sha": meta["head_sha"], "base_sha": meta["base_sha"]}


def load_manifest(run_dir: Path, root: Path) -> tuple[dict, bytes]:
    run_dir = run_dir.resolve()
    common = common_dir(root)
    if run_dir.parent != common / "yolo-agents-pr-gate/runs" or not re.fullmatch(r"[0-9a-f]{32}", run_dir.name):
        raise GateError("run-dir 不属于此 Git common-dir。")
    key = key_for(run_dir)
    manifest = read_json(run_dir / "manifest.json")
    verify_signature(manifest, key)
    if (manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "prepared"
            or manifest.get("run_id") != run_dir.name or manifest.get("product") != "yolo-trade"
            or manifest.get("common_dir") != str(common) or set(manifest.get("groups", {})) != set(GROUPS)):
        raise GateError("运行 manifest 不完整或产品不支持。")
    for group in GROUPS:
        if manifest["groups"][group] != str(run_dir / "worktrees" / group):
            raise GateError("工作树归属不匹配。")
    for label in ("head", "base"):
        if git(root, "rev-parse", f"refs/yolo-pr-gate/{run_dir.name}/{label}") != manifest["metadata"][f"{label}_sha"]:
            raise GateError("冻结 ref 已被修改。")
    return manifest, key


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 1800


def checks_for(group: str, manifest: dict, run_dir: Path) -> list[Check]:
    if manifest["product"] != "yolo-trade" or group not in GROUPS:
        raise GateError("产品或组缺少检查能力。")
    python = manifest["python"]
    build = run_dir / group / "build"
    meta = manifest["metadata"]
    if group == "p0":
        return [
            Check("feature-test-pairing", (python, "bin/check_feature_tests.py", "--base", meta["base_sha"], "--head", meta["head_sha"])),
            Check("repository-scripts", (python, "-m", "unittest", "discover", "-s", "bin/tests", "-p", "test_*.py")),
            Check("python-core", (python, "-m", "unittest", "discover", "-s", "packages/core-python/tests")),
            Check("python-product", (python, "-m", "unittest", "discover", "-s", "agents/yolo-trade/runtime-python/tests")),
            Check("agent-isolation", (python, "bin/test-agent-isolation.py")),
            Check("local-integration", (python, "bin/test-chat-integration.py", "--sdk-only")),
        ]
    if group == "mac":
        packages = (("swift-sdk", "packages/sdk-swift"), ("swift-ui", "packages/ui-swift"), ("swift-product", "apps/yolo-trade/client-swift"))
        checks = [Check(name, ("/usr/bin/xcrun", "swift", "test", "--package-path", package, "--scratch-path", str(build / name))) for name, package in packages]
        checks.append(Check("xcode-macos", (
            "/usr/bin/xcodebuild", "-project", "apps/yolo-trade/xcode/YoloTrade.xcodeproj", "-scheme", "YoloTradeMac",
            "-configuration", "Debug", "-destination", "platform=macOS,arch=arm64",
            "-derivedDataPath", str(build / "derived-macos"), "-skipMacroValidation", "ARCHS=arm64", "ONLY_ACTIVE_ARCH=YES",
            "SWIFT_ENABLE_EXPLICIT_MODULES=NO", "-quiet", "build",
        )))
        return checks
    return [
        Check("xcode-ios-simulator", (
            "/usr/bin/xcodebuild", "-project", "apps/yolo-trade/xcode/YoloTrade.xcodeproj", "-scheme", "YoloTradeIOS",
            "-configuration", "Debug", "-destination", "generic/platform=iOS Simulator",
            "-derivedDataPath", str(build / "derived-ios"), "-skipMacroValidation", "ARCHS=arm64", "ONLY_ACTIVE_ARCH=YES",
            "SWIFT_ENABLE_EXPLICIT_MODULES=NO", "-quiet", "build",
        )),
        Check("ios-hosted-sdk", (python, "bin/test_yolo_trade_ios_simulator.py", "--derived-data", str(build / "derived-ios-tests"))),
    ]


def environment(worktree: Path, output: Path) -> dict[str, str]:
    # Do not inherit API keys, provider tokens, product config, or PYTHONPATH.
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "DEVELOPER_DIR", "TMPDIR")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PYTHONPATH"] = os.pathsep.join(str(worktree / path) for path in ("packages/core-python", "agents/yolo-trade/runtime-python"))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["DEVELOPER_DIR"] = env.get("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")
    env["CLANG_MODULE_CACHE_PATH"] = str(output / "build/clang-cache")
    env["SWIFTPM_MODULECACHE_OVERRIDE"] = str(output / "build/swift-cache")
    return env


def execute_check(check: Check, worktree: Path, output: Path, *, dry_run: bool = False) -> dict:
    log_path = output / "logs" / f"{check.name}.log"
    started = time.monotonic()
    code, status, reason = None, "not_run", "dry_run" if dry_run else ""
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as log:
        if dry_run:
            log.write(b"dry run: check was not executed\n")
        else:
            try:
                env = environment(worktree, output)
                if check.name.startswith("swift-") or check.name in ("local-integration", "xcode-macos"):
                    env["SDKROOT"] = str(Path(env["DEVELOPER_DIR"]) / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk")
                process = subprocess.Popen(check.command, cwd=worktree, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=check.timeout_seconds)
                    status = "passed" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    # Kill the entire test/build process group, including grandchildren.
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    status, reason = "failed", "timeout"
                    log.write(b"\nPR gate timeout; process group stopped.\n")
                except KeyboardInterrupt:
                    # The deterministic controller may cancel a stale PR run.
                    # Stop only this check's owned process group, then propagate
                    # so run_group cannot publish an interrupted passing report.
                    try:
                        os.killpg(process.pid, signal.SIGINT)
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    except ProcessLookupError:
                        pass
                    raise
            except OSError:
                status, reason = "blocked", "could_not_start"
                log.write(b"\nPR gate could not start command.\n")
    return {
        "name": check.name, "command": list(check.command), "timeout_seconds": check.timeout_seconds,
        "status": status, "exit_code": code, "reason": reason,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log": str(log_path.relative_to(output.parent)),
        "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
    }


def identity(manifest: dict) -> dict:
    return {"schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"], "product": manifest["product"],
            "head_sha": manifest["metadata"]["head_sha"], "base_sha": manifest["metadata"]["base_sha"]}


def run_group(root: Path, run_dir: Path, group: str, *, dry_run: bool = False) -> dict:
    run_dir = run_dir.resolve()
    manifest, key = load_manifest(run_dir, root)
    if group not in GROUPS:
        raise GateError("无效检查组。")
    output = run_dir / group
    worktree = Path(manifest["groups"][group])
    lock_root = Path(manifest["common_dir"]) / "yolo-agents-pr-gate/locks"
    # Locks live in the shared common-dir, so other linked worktrees cooperate.
    with ExitStack() as stack:
        stack.enter_context(lock(lock_root / f"{manifest['run_id']}-{group}.lock"))
        if group == "simulator":
            stack.enter_context(lock(lock_root / f"{manifest['product']}-ui.lock"))
            stack.enter_context(lock(lock_root / "simulator.lock"))
        if (output / "report.json").exists():
            raise GateError("此组已有报告；重跑请 prepare 新 run。")
        report = dict(identity(manifest), group=group, dry_run=dry_run, started_at=now(), checks=[])
        try:
            assert_refs(root, manifest["metadata"])
            assert_snapshot(worktree, report["head_sha"])
            report["before_clean"] = True
            for check in checks_for(group, manifest, run_dir):
                report["checks"].append(execute_check(check, worktree, output, dry_run=dry_run))
            assert_snapshot(worktree, report["head_sha"])
            assert_refs(root, manifest["metadata"])
            report["after_clean"] = True
            statuses = {item["status"] for item in report["checks"]}
            report["status"] = "not_run" if dry_run else "failed" if "failed" in statuses else "blocked" if "blocked" in statuses else "passed"
        except (GateError, OSError) as error:
            report["status"], report["reason"] = "blocked", str(error) if isinstance(error, GateError) else "检查输出不可用。"
        report["finished_at"] = now()
        write_json(output / "report.json", signed(report, key))
        return report


def validate_group(report: dict, group: str, manifest: dict, run_dir: Path, key: bytes) -> None:
    verify_signature(report, key)
    expected = dict(identity(manifest), group=group, status="passed", dry_run=False, before_clean=True, after_clean=True)
    if any(report.get(name) != value or type(report.get(name)) is not type(value) for name, value in expected.items()):
        raise GateError("检查组报告身份、版本、状态或清洁度不匹配。")
    checks = report.get("checks")
    plan = checks_for(group, manifest, run_dir)
    if not isinstance(checks, list) or len(checks) != len(plan):
        raise GateError("检查组漏项或存在重复项。")
    for actual, required in zip(checks, plan):
        if (not isinstance(actual, dict) or actual.get("name") != required.name or actual.get("command") != list(required.command)
                or actual.get("status") != "passed" or type(actual.get("exit_code")) is not int or actual["exit_code"] != 0
                or actual.get("reason") != "" or actual.get("timeout_seconds") != required.timeout_seconds):
            raise GateError("检查计划、结果、顺序或退出状态不匹配。")
        expected_log = f"{group}/logs/{required.name}.log"
        if actual.get("log") != expected_log:
            raise GateError("检查日志归属错误。")
        log_path = run_dir / expected_log
        if log_path.is_symlink() or not log_path.is_file() or hashlib.sha256(log_path.read_bytes()).hexdigest() != actual.get("log_sha256"):
            raise GateError("检查日志丢失或被修改。")


def validate_ui(report: dict, manifest: dict) -> None:
    """Public report contract for bin/test_yolo_trade_pr_ui.py.

    The UI runner, not an SDK/probe runner, must emit these three evidence
    checks. Main runs it after all groups. A dry run can never meet this gate.
    """
    expected = dict(identity(manifest), group="ui", status="passed")
    if any(report.get(name) != value or type(report.get(name)) is not type(value) for name, value in expected.items()):
        raise GateError("UI 报告缺失真实 App 验收声明或 snapshot 不一致。")
    # This runner has no dry-run mode. Older SDK/probe reports cannot satisfy
    # its ui group plus the three specific real-App workflow checks.
    if report.get("dry_run", False) is not False or report.get("production_probe", False) is not False:
        raise GateError("dryrun/probe 不能作为 UI 验收。")
    checks = report.get("checks")
    names = ("mac_model_reply", "simulator_model_reply", "bidirectional_sync")
    if (not isinstance(checks, list) or len(checks) != len(names)
            or not all(isinstance(item, dict) for item in checks)
            or {item.get("name") for item in checks} != set(names)):
        raise GateError("UI 报告必须包含双端对话及同步三个独立结果。")
    for check in checks:
        if check.get("status") != "passed":
            raise GateError("UI 检查未通过或只是 probe。")


def conflict_free(root: Path, meta: dict, run_dir: Path) -> bool:
    # merge-tree writes only disposable objects, never the repository object DB.
    objects = run_dir / "merge-check-objects"
    private_dir(objects)
    original = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OBJECT_DIRECTORY=str(objects), GIT_ALTERNATE_OBJECT_DIRECTORIES=str(original))
    result = subprocess.run(["git", "merge-tree", "--write-tree", meta["base_sha"], meta["head_sha"]], cwd=root, env=env, capture_output=True, check=False, timeout=60)
    if result.returncode not in (0, 1):
        raise GateError("无法确定合并冲突；需要支持 merge-tree --write-tree 的 Git。")
    return result.returncode == 0


def summary(root: Path, run_dir: Path, metadata_path: Path, ui_report: Path) -> dict:
    run_dir = run_dir.resolve()
    manifest, key = load_manifest(run_dir, root)
    result = dict(identity(manifest), merge_ready=False, status="blocked", created_at=now(), reasons=[])
    try:
        current = metadata(metadata_path)
        if current != manifest["metadata"]:
            raise GateError("官方最新 metadata 的 head/base/PR 已变化；必须重新 prepare 并检查。")
        age = time.time() - metadata_path.stat().st_mtime
        if not 0 <= age <= 120:
            raise GateError("summary metadata 必须是最近 120 秒内重新从官方 API 导出的文件。")
        remote, repository = remote_repository(root)
        if remote != manifest["origin"] or repository.lower() != current["repository"].lower():
            raise GateError("SSH 远端已变化。")
        assert_refs(root, current)
        assert_snapshot(Path(manifest["source_worktree"]), current["head_sha"])
        latest_finished = 0.0
        for group in GROUPS:
            report = read_json(run_dir / group / "report.json")
            validate_group(report, group, manifest, run_dir, key)
            assert_snapshot(Path(manifest["groups"][group]), current["head_sha"])
            latest_finished = max(latest_finished, datetime.fromisoformat(report["finished_at"]).timestamp())
        ui = read_json(ui_report)
        validate_ui(ui, manifest)
        if ui_report.resolve() != run_dir / "ui/report.json":
            raise GateError("UI 报告必须保存在本 run 的 ui/report.json。")
        if ui_report.stat().st_mtime < latest_finished:
            raise GateError("UI 验收必须在三个检查组完成后运行。")
        # A refresh made before checks completed is insufficient even within 120s.
        if metadata_path.stat().st_mtime < max(latest_finished, ui_report.stat().st_mtime):
            raise GateError("需要在全部检查及 UI 验收完成后重新导出 metadata。")
        if not conflict_free(root, current, run_dir):
            raise GateError("当前 base/head 存在合并冲突。")
        # Recheck after the potentially long conflict check before issuing a
        # merge-ready receipt. Main must still refresh the API before merging.
        load_manifest(run_dir, root)
        assert_refs(root, current)
        assert_snapshot(Path(manifest["source_worktree"]), current["head_sha"])
        for group in GROUPS:
            assert_snapshot(Path(manifest["groups"][group]), current["head_sha"])
        if metadata(metadata_path) != current or not 0 <= time.time() - metadata_path.stat().st_mtime <= 120:
            raise GateError("summary 期间 metadata 已变化或过期。")
        result.update(merge_ready=True, status="passed")
    except (GateError, OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
        result["reasons"].append(str(error) if isinstance(error, GateError) else "报告无效、缺失或冲突检查超时。")
    write_json(run_dir / "summary.json", signed(result, key), exclusive=False)
    return result


def cleanup(root: Path, run_dir: Path) -> dict:
    run_dir = run_dir.resolve()
    manifest, _ = load_manifest(run_dir, root)
    removed = []
    build_artifacts_removed = []
    lock_root = Path(manifest["common_dir"]) / "yolo-agents-pr-gate/locks"
    with ExitStack() as stack:
        stack.enter_context(lock(lock_root / "prepare.lock"))
        for group in GROUPS:
            stack.enter_context(lock(lock_root / f"{manifest['run_id']}-{group}.lock"))
        build_directories = []
        for group in GROUPS:
            worktree = Path(manifest["groups"][group])
            if worktree.exists():
                assert_snapshot(worktree, manifest["metadata"]["head_sha"])
            output = run_dir / group
            build = output / "build"
            if output.is_symlink() or not output.is_dir():
                raise GateError("检查组输出目录无效；拒绝清理。")
            if build.is_symlink() or (build.exists() and not build.is_dir()):
                raise GateError("构建目录无效；拒绝清理。")
            build_directories.append((group, build))
        for group in GROUPS:
            worktree = Path(manifest["groups"][group])
            if worktree.exists():
                git(root, "worktree", "remove", str(worktree))
                removed.append(group)
        for group, build in build_directories:
            if build.exists():
                shutil.rmtree(build)
                build_artifacts_removed.append(group)
    return {
        "status": "cleaned",
        "removed": removed,
        "build_artifacts_removed": build_artifacts_removed,
        "reports_retained": str(run_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare_parser = commands.add_parser("prepare", help="main only: freeze refs and create three detached worktrees")
    prepare_parser.add_argument("--metadata", type=Path, required=True)
    prepare_parser.add_argument("--product", required=True)
    runner = commands.add_parser("run-group", help="one read-only test chat per group; no Git writes")
    runner.add_argument("--run-dir", type=Path, required=True)
    runner.add_argument("--group", choices=GROUPS, required=True)
    runner.add_argument("--dry-run", action="store_true")
    summary_parser = commands.add_parser("summary", help="main only: use freshly exported official PR metadata")
    summary_parser.add_argument("--run-dir", type=Path, required=True)
    summary_parser.add_argument("--metadata", type=Path, required=True)
    summary_parser.add_argument("--ui-report", type=Path, required=True)
    clean = commands.add_parser("cleanup", help="main only: remove only this run's clean worktrees; retain reports")
    clean.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            value = prepare(args.root, args.metadata, args.product)
        elif args.action == "run-group":
            value = run_group(args.root, args.run_dir, args.group, dry_run=args.dry_run)
        elif args.action == "summary":
            value = summary(args.root, args.run_dir, args.metadata, args.ui_report)
        else:
            value = cleanup(args.root, args.run_dir)
    except (GateError, OSError, ValueError) as error:
        value = {"status": "blocked", "merge_ready": False, "reason": str(error) if isinstance(error, GateError) else "文件或运行参数无效。"}
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0 if value["status"] in ("prepared", "passed", "cleaned") else 1


if __name__ == "__main__":
    raise SystemExit(main())
