"""Semantic tests for the PR UI gate; no App, Host, network or Simulator is started."""
from __future__ import annotations

from dataclasses import replace
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

SPEC = importlib.util.spec_from_file_location("yolo_trade_pr_ui", Path(__file__).resolve().parents[1] / "test_yolo_trade_pr_ui.py")
ui = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ui
SPEC.loader.exec_module(ui)
RUN_ID = "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"
HEAD = "b" * 40
BASE = "c" * 40
CONVERSATION = "40000000-0000-4000-8000-000000000004"


def session():
    return ui.validate_session({"schema_version": 1, "expires_at": "2099-01-01T00:00:00+00:00", "run_id": RUN_ID, "endpoint": "http://127.0.0.1:49152",
                                "mac_pairing_code": "fixture-mac-once", "ios_pairing_code": "fixture-ios-once",
                                "test_conversation_id": CONVERSATION}, RUN_ID)


def evidence(phase="mac_send"):
    flags = {"mac_prompt_seen": True, "mac_reply_seen": True}
    if phase in ("ios_sync_send", "mac_verify"):
        flags.update(ios_prompt_seen=True, ios_reply_seen=True)
    if phase.endswith("cleanup"):
        flags = {"cleanup_complete": True}
    return {"schema_version": 1, "run_id": RUN_ID, "phase": phase,
            "conversation_hash": session()["conversation_hash"], "flags": flags}


class FakeWorkflow:
    def __init__(self, fail=None, cleanup=True, post_change=False):
        self.calls = []
        self.fail = fail
        self.cleaned = cleanup
        self.post_change = post_change

    def step(self, name):
        self.calls.append(name)
        if name == self.fail:
            raise ui.UIError("fixture_failure", blocked=name == "preflight")

    def check_revision(self):
        if self.post_change and "cleanup" in self.calls:
            raise ui.UIError("revision_mismatch")
        self.step("revision")

    def preflight(self): self.step("preflight")
    def create_simulator(self): self.step("simulator")
    def build(self, platform): self.step("build_" + platform)
    def start_host(self): self.step("host")
    def phase(self, phase): self.step(phase); return evidence(phase)
    def cleanup(self): self.step("cleanup"); return self.cleaned


class PRUIGateTests(unittest.TestCase):
    def test_simulator_type_must_be_supported_by_selected_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = {"isAvailable": True, "version": "27.1",
                       "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-27-1",
                       "supportedDeviceTypes": [{"identifier": "phone-supported"}]}
            phones = [{"identifier": identifier, "productFamily": "iPhone",
                       "minRuntimeVersionString": "27.0"}
                      for identifier in ["phone-supported", "phone-unsupported-later"]]
            calls = []
            def execute(command, **_):
                calls.append(command)
                if command[2:4] == ["list", "runtimes"]:
                    return json.dumps({"runtimes": [runtime]}).encode()
                if command[2:4] == ["list", "devicetypes"]:
                    return json.dumps({"devicetypes": phones}).encode()
                if command[2] == "create":
                    return b"40000000-0000-4000-8000-000000000004\n"
                return b""
            workflow = ui.UIWorkflow(self.options(directory), Path(directory), execute=execute)
            workflow.create_simulator()
            created = next(command for command in calls if command[2] == "create")
            self.assertEqual(created[4:], ["phone-supported", runtime["identifier"]])
            for supported in [[], [{"identifier": "uninstalled-phone"}]]:
                runtime["supportedDeviceTypes"] = supported
                calls.clear()
                with self.assertRaises(ui.UIError) as caught:
                    workflow.create_simulator()
                self.assertEqual(caught.exception.status, "blocked")
                self.assertFalse(any(command[2] == "create" for command in calls))

    def options(self, directory):
        return ui.Options(run_dir=Path(directory) / "report", head_sha=HEAD, base_sha=BASE,
                          credentials_data_dir=Path(directory), run_id=RUN_ID, root=Path(directory))

    def gate(self, directory, workflow):
        options = self.options(directory)
        result = ui.run_ui_gate(options, workflow_factory=lambda *_: workflow, lock_factory=lambda _: nullcontext())
        stored = json.loads((options.run_dir / "report.json").read_text())
        self.assertEqual(stored, result)
        self.assertEqual((options.run_dir / "report.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(options.run_dir.iterdir()), [options.run_dir / "report.json"])
        return result

    def test_real_ui_phases_are_ordered_and_only_all_three_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = FakeWorkflow()
            report = self.gate(directory, workflow)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["group"], "ui")
            self.assertEqual((report["head_sha"], report["base_sha"]), (HEAD, BASE))
            self.assertEqual([check["name"] for check in report["checks"]], list(ui.CHECK_NAMES))
            self.assertEqual(workflow.calls, ["revision", "preflight", "simulator", "build_macos", "build_ios", "host",
                                              "mac_send", "ios_sync_send", "mac_verify", "cleanup", "revision"])

    def test_failure_blocks_later_stages_but_always_cleans_and_rechecks_head(self):
        for failure in ["simulator", "build_macos", "build_ios", "host", "mac_send", "ios_sync_send", "mac_verify"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                workflow = FakeWorkflow(fail=failure)
                report = self.gate(directory, workflow)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(workflow.calls[-2:], ["cleanup", "revision"])
                if failure != "mac_verify":
                    self.assertNotIn("mac_verify", workflow.calls)

    def test_missing_dependency_is_blocked_and_never_launches_app_or_host(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = FakeWorkflow(fail="preflight")
            report = self.gate(directory, workflow)
            self.assertEqual(report["status"], "blocked")
            self.assertNotIn("host", workflow.calls)
            self.assertNotIn("build_macos", workflow.calls)
            self.assertEqual(report["checks"][1]["status"], "not_run")

    def test_cleanup_failure_or_post_test_dirty_tree_cannot_pass(self):
        for workflow in [FakeWorkflow(cleanup=False), FakeWorkflow(post_change=True), FakeWorkflow(fail="cleanup")]:
            with tempfile.TemporaryDirectory() as directory:
                report = self.gate(directory, workflow)
                self.assertEqual(report["status"], "failed")
                self.assertTrue(any(check["status"] == "failed" for check in report["checks"]))

    def test_skipped_zero_failed_expected_failure_and_missing_counts_are_rejected(self):
        good = {"result": "Passed", "totalTestCount": 1, "passedTests": 1, "failedTests": 0,
                "skippedTests": 0, "expectedFailures": 0}
        ui.validate_summary(good)
        for key, value in [("result", "Skipped"), ("totalTestCount", 0), ("passedTests", 0), ("failedTests", 1),
                           ("skippedTests", 1), ("expectedFailures", 1), ("passedTests", True)]:
            with self.subTest(key=key, value=value), self.assertRaises(ui.UIError):
                ui.validate_summary({**good, key: value})
        with self.assertRaises(ui.UIError):
            ui.validate_summary({})

    def test_ui_evidence_requires_both_real_replies_in_the_exact_same_conversation(self):
        for phase in ["mac_send", "ios_sync_send", "mac_verify", "mac_cleanup", "ios_cleanup"]:
            ui.validate_evidence(evidence(phase), session(), phase)
        for key, value in [("run_id", "b" * 32), ("phase", "ios_sync_send"), ("conversation_hash", "d" * 64),
                           ("flags", {"mac_reply_seen": True}), ("schema_version", 0)]:
            with self.subTest(key=key), self.assertRaises(ui.UIError):
                ui.validate_evidence({**evidence(), key: value}, session(), "mac_send")
        ios = evidence("ios_sync_send")
        ios["flags"]["ios_reply_seen"] = False
        with self.assertRaises(ui.UIError):
            ui.validate_evidence(ios, session(), "ios_sync_send")

    def test_session_requires_private_ephemeral_pairing_and_dedicated_conversation(self):
        original = session()
        for key, value in [("run_id", "wrong"), ("schema_version", True), ("expires_at", "2000-01-01T00:00:00+00:00"),
                           ("endpoint", "https://example.test"),
                           ("endpoint", "http://127.0.0.1:49152?token=x"), ("test_conversation_id", "missing"),
                           ("ios_pairing_code", original["mac_pairing_code"]), ("mac_pairing_code", "x\ny")]:
            with self.subTest(key=key), self.assertRaises(ui.UIError):
                ui.validate_session({**original, key: value}, RUN_ID)
        for key in ["run_id", "endpoint", "test_conversation_id", "mac_pairing_code", "ios_pairing_code"]:
            missing = dict(original); missing.pop(key)
            with self.assertRaises(ui.UIError):
                ui.validate_session(missing, RUN_ID)

    def test_session_and_reports_reject_symlinks_and_public_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "session.json"
            ui.private_write(target, json.dumps(session()).encode())
            self.assertEqual(ui.private_json(target)["run_id"], RUN_ID)
            target.chmod(0o644)
            with self.assertRaises(ui.UIError): ui.private_json(target)
            link = root / "link"; link.symlink_to(target)
            with self.assertRaises(OSError): ui.private_write(link, b"replace")
            self.assertIn("run_id", target.read_text())

    def test_only_one_real_ui_target_receives_private_launch_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xctestrun"
            destination = root / "private.xctestrun"
            app = root / "App.app" / "Contents"
            app.mkdir(parents=True)
            bundle = "com.yoloagents.yolotrade.prtest." + RUN_ID
            (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle, "YoloTradePRTestBuild": "YES"}))
            target = {"TestBundlePath": "__TESTROOT__/UITests.xctest", "IsUITestBundle": True,
                      "UITargetAppPath": "__TESTROOT__/App.app"}
            source.write_bytes(plistlib.dumps({"TestConfigurations": [{"TestTargets": [target]}]}))
            ui.configured_xctestrun(source, destination, {ui.PREFIX + "PAIRING_CODE": "fixture-secret"}, expected_bundle_id=bundle)
            configured = plistlib.loads(destination.read_bytes())["TestConfigurations"][0]["TestTargets"][0]
            self.assertEqual(configured["EnvironmentVariables"][ui.PREFIX + "PAIRING_CODE"], "fixture-secret")
            self.assertNotIn("__TESTROOT__", configured["TestBundlePath"])
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            for targets in [[{**target, "IsUITestBundle": False}], [], [target, target]]:
                source.write_bytes(plistlib.dumps({"TestConfigurations": [{"TestTargets": targets}]}))
                with self.assertRaises(ui.UIError): ui.configured_xctestrun(source, destination, {}, expected_bundle_id=bundle)

    def test_missing_or_duplicate_ui_attachment_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence.json").write_text(json.dumps(evidence()))
            attachment = {"suggestedHumanReadableName": "pr-ui-evidence.json", "exportedFileName": "evidence.json"}
            manifest = [{"attachments": [attachment]}]
            (root / "manifest.json").write_text(json.dumps(manifest))
            self.assertEqual(ui.read_evidence(root), evidence())
            for attachments in [[], [attachment, attachment], [{**attachment, "exportedFileName": "../secret.json"}]]:
                (root / "manifest.json").write_text(json.dumps([{"attachments": attachments}]))
                with self.assertRaises(ui.UIError): ui.read_evidence(root)

    def test_timeout_kills_only_the_owned_command_group_and_hides_command_text(self):
        process = Mock(pid=123456, returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired(["fixture-secret"], 1)
        process.wait.return_value = 0
        with patch.object(ui.subprocess, "Popen", return_value=process), patch.object(ui.os, "killpg") as kill:
            with self.assertRaises(ui.UIError) as caught:
                ui.execute_command(["fixture-secret"], cwd=Path("/tmp"), env={}, timeout=1)
        kill.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual(str(caught.exception), "command_timeout")
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_actual_cleanup_attempts_all_resources_even_if_app_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = []
            def execute(command, **_):
                commands.append(command)
                return b""
            workflow = ui.UIWorkflow(self.options(directory), Path(directory), execute=execute)
            workflow.session = session()
            workflow.builds = {"macos": Path("mac"), "ios": Path("ios")}
            workflow.device = str(UUID(CONVERSATION))
            workflow.host = Mock()
            workflow.phase = Mock(side_effect=ui.UIError("namespace_cleanup_failed"))
            with patch.object(ui, "terminate_group") as terminate:
                self.assertFalse(workflow.cleanup())
            terminate.assert_called_once_with(workflow.host)
            self.assertEqual([call.args[0] for call in workflow.phase.call_args_list], ["mac_cleanup", "ios_cleanup"])
            self.assertTrue(any("shutdown" in command for command in commands))
            self.assertTrue(any("delete" in command for command in commands))


    def test_production_app_or_missing_test_marker_cannot_be_launched_by_xcode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Candidate.app"
            app.mkdir()
            expected = "com.yoloagents.yolotrade.prtest." + RUN_ID
            info = app / "Info.plist"
            for metadata in [{"CFBundleIdentifier": "com.yoloagents.yolotrade", "YoloTradePRTestBuild": "YES"},
                             {"CFBundleIdentifier": expected, "YoloTradePRTestBuild": "NO"}]:
                info.write_bytes(plistlib.dumps(metadata))
                with self.assertRaises(ui.UIError):
                    ui.validate_app_bundle(app, root, expected, test_app=True)
            info.write_bytes(plistlib.dumps({"CFBundleIdentifier": expected, "YoloTradePRTestBuild": "YES"}))
            ui.validate_app_bundle(app, root, expected, test_app=True)
            with self.assertRaises(ui.UIError):
                ui.validate_app_bundle(app, root / "other-build", expected, test_app=True)

    def test_head_and_base_and_clean_tree_are_checked_without_git_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            commands = []
            def execute(command, **_):
                commands.append(command)
                if command == ["git", "rev-parse", "HEAD"]: return HEAD.encode()
                if "--verify" in command: return BASE.encode()
                return b""
            workflow = ui.UIWorkflow(self.options(directory), Path(directory), execute=execute)
            workflow.check_revision()
            self.assertTrue(any("--is-ancestor" in command for command in commands))
            self.assertTrue(any("status" in command for command in commands))
            self.assertFalse(any(part in {"checkout", "reset", "add", "commit", "push"} for command in commands for part in command))
            workflow.options = replace(workflow.options, head_sha="d" * 40)
            with self.assertRaises(ui.UIError): workflow.check_revision()


    def test_host_run_directory_is_empty_and_log_lives_outside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            options = self.options(directory)
            captured = []
            process = Mock()
            process.poll.return_value = None
            def spawn(command, **kwargs):
                host_root = Path(command[command.index("--run-dir") + 1])
                self.assertEqual(list(host_root.iterdir()), [])
                self.assertEqual(Path(kwargs["stdout"].name), Path(directory) / "host.log")
                self.assertEqual(Path(kwargs["stdout"].name).stat().st_mode & 0o777, 0o600)
                self.assertEqual(kwargs["env"]["PYTHONPATH"], os.pathsep.join([
                    str(options.root / "packages/core-python"),
                    str(options.root / "agents/yolo-trade/runtime-python")]))
                ui.private_write(host_root / "session.json", json.dumps(session()).encode())
                captured.append(command)
                return process
            workflow = ui.UIWorkflow(options, Path(directory), spawn=spawn)
            try:
                workflow.start_host()
                self.assertEqual(workflow.session["run_id"], RUN_ID)
                self.assertIn("yolo_trade.pr_test_host", captured[0])
            finally:
                if workflow.host_log is not None: workflow.host_log.close()


    def test_missing_session_times_out_without_opening_any_app(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock()
            process.poll.return_value = None
            ticks = iter([0, 1, 100])
            workflow = ui.UIWorkflow(self.options(directory), Path(directory),
                                     spawn=lambda *_args, **_kwargs: process,
                                     clock=lambda: next(ticks), sleep=lambda _: None)
            try:
                with self.assertRaises(ui.UIError) as caught:
                    workflow.start_host()
                self.assertEqual(caught.exception.status, "blocked")
                self.assertEqual(caught.exception.reason, "temporary_host_session_timeout")
                self.assertEqual(workflow.builds, {})
                self.assertIsNone(workflow.session)
            finally:
                if workflow.host_log is not None: workflow.host_log.close()

    def test_shared_git_common_lock_blocks_another_worktree_before_resource_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            common = Path(directory) / "common.git"
            common.mkdir(mode=0o700)
            options = self.options(directory)
            other = replace(options, root=Path(directory) / "other-worktree")
            def execute(command, **_):
                self.assertEqual(command, ["git", "rev-parse", "--git-common-dir"])
                return str(common).encode()
            def lock(option): return ui.product_ui_lock(option, execute=execute)
            factory = Mock(side_effect=AssertionError("Must not create a workflow while locked"))
            with lock(options):
                self.assertFalse(options.run_dir.exists())
                report = ui.run_ui_gate(other, workflow_factory=factory, lock_factory=lock)
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(report["checks"][0]["reason"], "ui_product_lock_busy")
                factory.assert_not_called()
            with lock(other):
                pass
            target = common / "yolo-agents-pr-gate/locks/yolo-trade-ui.lock"
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
