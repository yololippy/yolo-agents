#!/usr/bin/env python3
"""Run isolated YoloTrade Mac/Simulator conversation XCUITests after a PR exists.

Only report.json is a publishable artifact. Temporary sessions, launch environments,
xcresults, build directories and logs remain private and are deleted on every exit.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
CHECK_NAMES = ("mac_model_reply", "simulator_model_reply", "bidirectional_sync")
PREFIX = "YOLO_TRADE_PR_UI_"


class UIError(Exception):
    def __init__(self, reason: str, *, blocked: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.status = "blocked" if blocked else "failed"


@dataclass(frozen=True)
class Options:
    run_dir: Path
    head_sha: str
    base_sha: str
    credentials_data_dir: Path
    run_id: str
    product: str = "yolo-trade"
    python: str = sys.executable
    root: Path = ROOT
    timeout: int = 600


def private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise UIError("unsafe_run_directory", blocked=True)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UIError("unsafe_run_directory", blocked=True)
    return path.resolve()


def private_write(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(content)


def private_json(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > 65536):
            raise UIError("unsafe_session", blocked=True)
        try:
            value = json.load(source)
        except (ValueError, UnicodeError):
            raise UIError("invalid_session", blocked=True) from None
    if not isinstance(value, dict):
        raise UIError("invalid_session", blocked=True)
    return value


def terminate_group(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def execute_command(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int) -> bytes:
    # A command and its output can contain launch credentials. Neither is ever
    # interpolated into an exception, stdout, report or public log.
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    except OSError:
        raise UIError("command_unavailable", blocked=True) from None
    try:
        output, _ = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        terminate_group(process)
        raise UIError("command_timeout") from None
    if process.returncode:
        raise UIError("command_failed")
    if len(output) > 32 * 1024 * 1024:
        raise UIError("command_output_limit")
    return output


def validate_session(session: dict, run_id: str) -> dict:
    try:
        address = urlsplit(session["endpoint"])
        conversation = UUID(session["test_conversation_id"])
        expires = datetime.fromisoformat(session["expires_at"])
        port = address.port
        valid = (type(session.get("schema_version")) is int and session["schema_version"] == 1 and session["run_id"] == run_id
                 and expires.tzinfo is not None and expires > datetime.now(timezone.utc)
                 and address.scheme == "http"
                 and address.hostname == "127.0.0.1" and port is not None
                 and 1024 <= port <= 65535
                 and not address.username and not address.password and not address.query
                 and not address.fragment and address.path in ("", "/")
                 and all(isinstance(session[key], str) and 6 <= len(session[key]) <= 256
                         and not any(character.isspace() for character in session[key])
                         for key in ("mac_pairing_code", "ios_pairing_code"))
                 and session["mac_pairing_code"] != session["ios_pairing_code"])
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise UIError("invalid_session", blocked=True)
    return {**session, "conversation_hash": hashlib.sha256(str(conversation).encode()).hexdigest()}


def validate_summary(summary: dict) -> None:
    expected = {"result": "Passed", "totalTestCount": 1, "passedTests": 1,
                "failedTests": 0, "skippedTests": 0, "expectedFailures": 0}
    if any(type(summary.get(key)) is not type(value) or summary.get(key) != value
           for key, value in expected.items()):
        raise UIError("ui_test_missing_failed_or_skipped")


def validate_evidence(evidence: dict, session: dict, phase: str) -> None:
    if (type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1 or evidence.get("run_id") != session["run_id"]
            or evidence.get("phase") != phase or not isinstance(evidence.get("flags"), dict)):
        raise UIError("ui_evidence_mismatch")
    if phase.endswith("cleanup"):
        if evidence["flags"].get("cleanup_complete") is not True:
            raise UIError("namespace_cleanup_failed")
        return
    required = ["mac_prompt_seen", "mac_reply_seen"]
    if phase in ("ios_sync_send", "mac_verify"):
        required += ["ios_prompt_seen", "ios_reply_seen"]
    if (evidence.get("conversation_hash") != session["conversation_hash"]
            or any(evidence["flags"].get(flag) is not True for flag in required)):
        raise UIError("ui_reply_or_shared_conversation_missing")


def validate_app_bundle(path: Path, allowed_root: Path, expected_bundle_id: str, *, test_app: bool) -> None:
    if not path.resolve().is_relative_to(allowed_root.resolve()):
        raise UIError("test_bundle_outside_private_build")
    metadata = path / "Contents/Info.plist" if (path / "Contents").is_dir() else path / "Info.plist"
    try:
        info = plistlib.loads(metadata.read_bytes())
    except (OSError, ValueError):
        raise UIError("test_app_metadata_missing") from None
    if info.get("CFBundleIdentifier") != expected_bundle_id:
        raise UIError("test_bundle_identity_mismatch")
    if test_app and info.get("YoloTradePRTestBuild") != "YES":
        raise UIError("test_build_marker_missing")


def configured_xctestrun(source: Path, destination: Path, environment: dict[str, str], *, expected_bundle_id: str) -> None:
    root = str(source.resolve().parent)
    value = plistlib.loads(source.read_bytes())
    targets = 0

    def configure(node):
        nonlocal targets
        if isinstance(node, str):
            return node.replace("__TESTROOT__", root)
        if isinstance(node, list):
            return [configure(item) for item in node]
        if isinstance(node, dict):
            result = {key: configure(item) for key, item in node.items()}
            if "TestBundlePath" in result:
                if result.get("IsUITestBundle") is not True:
                    raise UIError("non_ui_test_target")
                app = result.get("UITargetAppPath")
                if not isinstance(app, str):
                    raise UIError("ui_target_app_missing")
                validate_app_bundle(Path(app), source.resolve().parent, expected_bundle_id, test_app=True)
                if result.get("UITargetAppBundleIdentifier", expected_bundle_id) != expected_bundle_id:
                    raise UIError("ui_target_bundle_identity_mismatch")
                result.setdefault("EnvironmentVariables", {}).update(environment)
                # The app is launched explicitly by XCUITest with a strict allowlist.
                result.pop("UITargetAppEnvironmentVariables", None)
                targets += 1
            return result
        return node

    result = configure(value)
    if targets != 1:
        raise UIError("ui_target_not_unique")
    private_write(destination, plistlib.dumps(result))


def read_evidence(directory: Path) -> dict:
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
        matches = []
        for test in manifest:
            for attachment in test["attachments"]:
                if attachment.get("suggestedHumanReadableName") != "pr-ui-evidence.json":
                    continue
                relative = attachment["exportedFileName"]
                if not isinstance(relative, str) or Path(relative).name != relative:
                    raise UIError("invalid_ui_attachment")
                matches.append(json.loads((directory / relative).read_bytes()))
        if len(matches) != 1 or not isinstance(matches[0], dict):
            raise UIError("ui_evidence_missing")
        return matches[0]
    except (OSError, KeyError, TypeError, ValueError):
        raise UIError("ui_evidence_missing") from None


class UIWorkflow:
    def __init__(self, options: Options, private_root: Path, *, execute=execute_command,
                 spawn=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
        self.options = options
        self.private_root = private_root
        self.execute = execute
        self.spawn = spawn
        self.clock = clock
        self.sleep = sleep
        self.host = None
        self.host_log = None
        self.device: str | None = None
        self.session: dict | None = None
        self.builds: dict[str, Path] = {}
        self.derived = private_root / "derived"
        self.env = dict(os.environ)
        self.env.pop("SDKROOT", None)
        self.env.pop("YOLO_AGENTS_RUNNING_SDK_TESTS", None)
        # The temporary host must load the candidate's code, never another checkout.
        self.env["PYTHONPATH"] = os.pathsep.join([
            str(options.root / "packages/core-python"),
            str(options.root / "agents/yolo-trade/runtime-python"),
        ])
        self.env["DEVELOPER_DIR"] = self.env.get("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")

    def command(self, command: list[str], timeout: int | None = None) -> bytes:
        return self.execute(command, cwd=self.options.root, env=self.env,
                            timeout=timeout or self.options.timeout)

    def check_revision(self) -> None:
        options = self.options
        if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (options.head_sha, options.base_sha)):
            raise UIError("full_revision_required", blocked=True)
        head = self.command(["git", "rev-parse", "HEAD"], 30).decode().strip()
        base = self.command(["git", "rev-parse", "--verify", options.base_sha + "^{commit}"], 30).decode().strip()
        if head != options.head_sha or base != options.base_sha:
            raise UIError("revision_mismatch", blocked=True)
        self.command(["git", "merge-base", "--is-ancestor", options.base_sha, options.head_sha], 30)
        if self.command(["git", "status", "--porcelain=v1", "--untracked-files=all"], 30).strip():
            raise UIError("worktree_not_clean", blocked=True)

    def preflight(self) -> None:
        if (self.options.product != "yolo-trade" or
                re.fullmatch(r"[a-f0-9]{32}", self.options.run_id) is None
                or UUID(self.options.run_id).version != 4):
            raise UIError("unsupported_product_or_run_id", blocked=True)
        if not self.options.credentials_data_dir.is_dir():
            raise UIError("product_credentials_directory_required", blocked=True)
        # Credential provenance is verified by pr_test_host itself. This runner
        # accepts only a product data directory, never credential files or tokens.
        self.command(["/usr/bin/xcrun", "--find", "xcodebuild"], 30)
        self.command(["/usr/bin/xcrun", "--find", "simctl"], 30)
        self.command(["/usr/bin/xcrun", "--find", "xcresulttool"], 30)

    def create_simulator(self) -> None:
        try:
            runtimes = json.loads(self.command(["/usr/bin/xcrun", "simctl", "list", "runtimes", "--json"], 30))["runtimes"]
            types = json.loads(self.command(["/usr/bin/xcrun", "simctl", "list", "devicetypes", "--json"], 30))["devicetypes"]
            available = [runtime for runtime in runtimes if runtime.get("isAvailable") is True
                         and runtime.get("identifier", "").startswith("com.apple.CoreSimulator.SimRuntime.iOS-")
                         and int(runtime["version"].split(".")[0]) >= 26]
            runtime = max(available, key=lambda item: tuple(int(part) for part in item["version"].split(".")))
            supported = {item["identifier"] for item in runtime["supportedDeviceTypes"]}
            def version(value):
                parts = tuple(int(part) for part in value.split("."))
                return parts + (0,) * max(0, 3 - len(parts))
            runtime_version = version(runtime["version"])
            phones = [item for item in types if item.get("productFamily") == "iPhone"
                      and item["identifier"] in supported
                      and version(item.get("minRuntimeVersionString", "0")) <= runtime_version
                      <= version(item.get("maxRuntimeVersionString", "65535.255.255"))]
            phone = max(phones, key=lambda item: (version(item.get("minRuntimeVersionString", "0")), item["identifier"]))
        except (KeyError, TypeError, ValueError, IndexError):
            raise UIError("ios_simulator_dependency_missing", blocked=True) from None
        output = self.command(["/usr/bin/xcrun", "simctl", "create", "YoloTrade PR UI " + self.options.run_id,
                               phone["identifier"], runtime["identifier"]], 60)
        try:
            # simctl accepts either case, but Xcode destination matching requires
            # the uppercase CoreSimulator identifier.
            self.device = str(UUID(output.decode().strip())).upper()
        except (UnicodeError, ValueError):
            raise UIError("simulator_creation_failed") from None
        self.command(["/usr/bin/xcrun", "simctl", "boot", self.device], 60)
        self.command(["/usr/bin/xcrun", "simctl", "bootstatus", self.device, "-b"], 180)

    def start_host(self) -> None:
        host_root = private_directory(self.private_root / "host")
        log = self.private_root / "host.log"
        private_write(log, b"")
        self.host_log = log.open("ab")
        try:
            self.host = self.spawn([
                self.options.python, "-m", "yolo_trade.pr_test_host",
                "--run-dir", str(host_root), "--run-id", self.options.run_id,
                "--credentials-data-dir", str(self.options.credentials_data_dir.resolve()),
            ], cwd=self.options.root, env=self.env, stdin=subprocess.DEVNULL,
                stdout=self.host_log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            raise UIError("temporary_host_unavailable", blocked=True) from None
        deadline = self.clock() + 90
        while self.clock() < deadline:
            session = host_root / "session.json"
            if session.exists():
                self.session = validate_session(private_json(session), self.options.run_id)
                return
            if self.host.poll() is not None:
                raise UIError("temporary_host_or_product_account_unavailable", blocked=True)
            self.sleep(0.2)
        raise UIError("temporary_host_session_timeout", blocked=True)

    def build(self, platform: str) -> None:
        is_mac = platform == "macos"
        scheme = "YoloTradeMacPRUI" if is_mac else "YoloTradeIOSPRUI"
        derived = self.derived / platform
        destination = "platform=macOS,arch=arm64" if is_mac else f"platform=iOS Simulator,id={self.device}"
        self.command([
            "/usr/bin/xcrun", "xcodebuild", "-project", "apps/yolo-trade/xcode/YoloTrade.xcodeproj",
            "-scheme", scheme, "-configuration", "Debug", "-destination", destination,
            "-derivedDataPath", str(derived), "-skipMacroValidation", "SWIFT_ENABLE_EXPLICIT_MODULES=NO",
            "-disableAutomaticPackageResolution", "-onlyUsePackageVersionsFromResolvedFile",
            "PR_TEST_BUILD=YES", "PR_TEST_BUNDLE_SUFFIX=.prtest." + self.options.run_id,
            "ARCHS=arm64", "ONLY_ACTIVE_ARCH=YES", "CODE_SIGN_IDENTITY=-", "CODE_SIGN_STYLE=Manual",
            "-quiet", "build-for-testing",
        ])
        configurations = list((derived / "Build/Products").glob("*.xctestrun"))
        if len(configurations) != 1:
            raise UIError("ui_build_configuration_missing")
        products = derived / "Build/Products" / ("Debug" if is_mac else "Debug-iphonesimulator")
        expected = "com.yoloagents.yolotrade.prtest." + self.options.run_id
        app_name = "YoloTradeMac" if is_mac else "YoloTradeIOS"
        validate_app_bundle(products / (app_name + ".app"), derived, expected, test_app=True)
        runner_name = app_name + "PRUITests-Runner.app"
        validate_app_bundle(products / runner_name, derived,
                            expected + (".mac" if is_mac else ".ios") + ".pruitests.xctrunner", test_app=False)
        self.builds[platform] = configurations[0]

    def phase(self, phase: str) -> dict:
        if self.session is None:
            raise UIError("temporary_session_missing", blocked=True)
        platform = "ios" if phase.startswith("ios_") else "macos"
        source = self.builds[platform]
        phase_root = private_directory(self.private_root / phase)
        configuration = phase_root / "ui.xctestrun"
        environment = {
            PREFIX + "RUN_ID": self.options.run_id,
            PREFIX + "PHASE": phase,
            PREFIX + "ENDPOINT": self.session["endpoint"],
            PREFIX + "PAIRING_CODE": self.session["ios_pairing_code" if platform == "ios" else "mac_pairing_code"],
            PREFIX + "BUNDLE_ID": "com.yoloagents.yolotrade.prtest." + self.options.run_id,
        }
        configured_xctestrun(source, configuration, environment,
                               expected_bundle_id=environment[PREFIX + "BUNDLE_ID"])
        destination = "platform=macOS,arch=arm64" if platform == "macos" else f"platform=iOS Simulator,id={self.device}"
        result = phase_root / "result.xcresult"
        try:
            self.command([
                "/usr/bin/xcrun", "xcodebuild", "test-without-building", "-xctestrun", str(configuration),
                "-destination", destination, "-resultBundlePath", str(result),
                "-parallel-testing-enabled", "NO", "-maximum-concurrent-test-simulator-destinations", "1",
                "-quiet",
            ])
            summary = json.loads(self.command([
                "/usr/bin/xcrun", "xcresulttool", "get", "test-results", "summary", "--path", str(result), "--compact",
            ], 30))
            validate_summary(summary)
            attachments = phase_root / "attachments"
            self.command(["/usr/bin/xcrun", "xcresulttool", "export", "attachments", "--path", str(result),
                          "--output-path", str(attachments), "--filter", "pr-ui-evidence*"], 30)
            evidence = read_evidence(attachments)
            validate_evidence(evidence, self.session, phase)
            return evidence
        except (OSError, ValueError, TypeError, KeyError):
            raise UIError("ui_result_invalid") from None
        finally:
            # xcresult and xctestrun may contain launch environments or system
            # screenshots. They are never retained as report attachments.
            shutil.rmtree(phase_root, ignore_errors=True)

    def cleanup(self) -> bool:
        clean = True
        # Keep the Host alive until both apps have forgotten their run-scoped records.
        if self.session is not None:
            for platform in tuple(self.builds):
                try:
                    self.phase("mac_cleanup" if platform == "macos" else "ios_cleanup")
                except (Exception, KeyboardInterrupt):
                    clean = False
        if self.host is not None:
            try:
                terminate_group(self.host)
            except (Exception, KeyboardInterrupt):
                clean = False
        if self.host_log is not None:
            try:
                self.host_log.close()
            except OSError:
                clean = False
        # LaunchServices may leave an app outside xcodebuild's process group.
        # Terminate only executables inside this run's dedicated derived directory.
        try:
            process_list = self.command(["/bin/ps", "-axo", "pid=,comm="], 30).decode()
            for line in process_list.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    continue
                executable = Path(parts[1])
                if executable.is_absolute() and executable.resolve().is_relative_to(self.derived.resolve()):
                    try:
                        os.kill(int(parts[0]), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except (Exception, KeyboardInterrupt):
            clean = False
        if self.device is not None:
            for action in ("shutdown", "delete"):
                try:
                    self.command(["/usr/bin/xcrun", "simctl", action, self.device], 60)
                except (Exception, KeyboardInterrupt):
                    # A shutdown failure still attempts deletion, and never passes silently.
                    clean = False
        return clean


@contextmanager
def product_ui_lock(options: Options, *, execute=execute_command):
    environment = dict(os.environ)
    try:
        output = execute(["git", "rev-parse", "--git-common-dir"], cwd=options.root,
                         env=environment, timeout=30).decode().strip()
    except (UnicodeError, OSError):
        raise UIError("git_common_directory_unavailable", blocked=True) from None
    if not output:
        raise UIError("git_common_directory_unavailable", blocked=True)
    common = Path(output)
    if not common.is_absolute():
        common = options.root / common
    common = common.resolve()
    if not common.is_dir():
        raise UIError("git_common_directory_unavailable", blocked=True)
    directory = private_directory(common / "yolo-agents-pr-gate/locks")
    path = directory / "yolo-trade-ui.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise UIError("unsafe_product_lock", blocked=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UIError("ui_product_lock_busy", blocked=True) from None
        yield
    finally:
        os.close(descriptor)


def run_ui_gate(options: Options, *, workflow_factory: Callable = UIWorkflow,
                lock_factory: Callable | None = None) -> dict:
    report = {"schema_version": 1, "group": "ui", "run_id": options.run_id,
              "head_sha": options.head_sha, "base_sha": options.base_sha,
              "product": options.product, "status": "not_run", "checks": []}
    checks = {name: {"name": name, "status": "not_run"} for name in CHECK_NAMES}
    active = CHECK_NAMES[0]
    stage = "lock"
    lock_factory = lock_factory or product_ui_lock
    try:
        # This is acquired before any run directory, Host, Simulator or App exists.
        with lock_factory(options):
            run_dir = private_directory(options.run_dir)
            with tempfile.TemporaryDirectory(prefix="ui-private-", dir=run_dir) as temporary:
                workflow = workflow_factory(options, Path(temporary))
                verified_initial_revision = False
                try:
                    stage = "revision"
                    workflow.check_revision()
                    verified_initial_revision = True
                    stage = "preflight"
                    workflow.preflight()
                    stage = "simulator"
                    workflow.create_simulator()
                    stage = "build_macos"
                    workflow.build("macos")
                    stage = "build_ios"
                    workflow.build("ios")
                    stage = "host"
                    workflow.start_host()
                    stage = "mac_send"
                    workflow.phase("mac_send")
                    checks[active]["status"] = "passed"
                    active = CHECK_NAMES[1]
                    stage = "ios_sync_send"
                    workflow.phase("ios_sync_send")
                    checks[active]["status"] = "passed"
                    active = CHECK_NAMES[2]
                    stage = "mac_verify"
                    workflow.phase("mac_verify")
                    checks[active]["status"] = "passed"
                    report["status"] = "passed"
                except UIError as error:
                    report["status"] = error.status
                    checks[active].update(status=error.status, reason=error.reason, stage=stage)
                except (OSError, ValueError, KeyError, TypeError, KeyboardInterrupt):
                    report["status"] = "failed"
                    checks[active].update(status="failed", reason="ui_workflow_failed", stage=stage)
                finally:
                    try:
                        cleaned = workflow.cleanup()
                    except (Exception, KeyboardInterrupt):
                        cleaned = False
                    if not cleaned:
                        report["status"] = "failed"
                        checks["cleanup"] = {"name": "cleanup", "status": "failed", "reason": "private_resource_cleanup_failed"}
                    if verified_initial_revision:
                        try:
                            workflow.check_revision()
                        except (UIError, OSError):
                            report["status"] = "failed"
                            checks["revision"] = {"name": "revision", "status": "failed", "reason": "revision_or_worktree_changed"}
    except UIError as error:
        report["status"] = error.status
        checks[active].update(status=error.status, reason=error.reason, stage=stage)
    except (OSError, ValueError, KeyError, TypeError, KeyboardInterrupt):
        report["status"] = "failed"
        checks[active].update(status="failed", reason="ui_workflow_failed", stage=stage)
    finally:
        report["checks"] = list(checks.values())
        if report["status"] == "passed" and any(check["status"] != "passed" for check in report["checks"]):
            report["status"] = "failed"
        # Lock contention creates only this caller's sanitized report; it never
        # creates a workflow or cleans the current lock owner's resources.
        run_dir = private_directory(options.run_dir)
        private_write(run_dir / "report.json", (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--product", default="yolo-trade")
    parser.add_argument("--credentials-data-dir", type=Path, required=True)
    parser.add_argument("--python", default=str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").is_file() else sys.executable)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    if not 30 <= args.timeout <= 3600:
        parser.error("--timeout must be between 30 and 3600 seconds")
    options = Options(run_dir=args.run_dir, head_sha=args.head_sha, base_sha=args.base_sha,
                      credentials_data_dir=args.credentials_data_dir, run_id=args.run_id or uuid4().hex,
                      product=args.product, python=args.python, timeout=args.timeout)
    try:
        report = run_ui_gate(options)
    except (UIError, OSError):
        print("YoloTrade UI gate could not create a private report.", file=sys.stderr)
        return 1
    print(json.dumps({"group": "ui", "status": report["status"], "run_id": report["run_id"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
