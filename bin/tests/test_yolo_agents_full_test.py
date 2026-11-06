from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yolo_agents_full_test as full_test


class FullTestRunnerTests(unittest.TestCase):
    def test_automated_plan_is_ordered_and_product_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            checks = full_test.automated_checks("yolo-trade", Path(directory))
        self.assertEqual([item.name for item in checks], [
            "full-test-runner", "python-core", "python-yolo-trade", "agent-isolation", "swift-sdk", "swift-ui",
            "swift-yolo-trade", "swift-python-integration", "xcode-macos", "xcode-ios-simulator",
            "xcode-ios-simulator-tests", "xcode-ios-device",
        ])
        command_text = "\n".join(" ".join(item.command) for item in checks)
        self.assertIn("agents/yolo-trade", command_text)
        self.assertIn("test_yolo_trade_ios_simulator.py", command_text)
        self.assertNotIn("YoloBeni.xcodeproj", command_text)

    def test_runner_continues_after_failure_and_records_each_log(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return Mock(returncode=7 if len(calls) == 1 else 0)
        checks = [full_test.Check("first", ("false",)), full_test.Check("second", ("true",))]
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory)
            results = [full_test.run_check(check, report, run=run) for check in checks]
            self.assertTrue(all((report / item.log).is_file() for item in results))
        self.assertEqual([item.status for item in results], ["failed", "passed"])
        self.assertEqual(len(calls), 2)

    def test_timeout_is_a_failure_instead_of_a_skip(self):
        def run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["fixture"], 1)
        with tempfile.TemporaryDirectory() as directory:
            result = full_test.run_check(full_test.Check("timeout", ("fixture",), 1), Path(directory), run=run)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.exit_code)

    def test_missing_tool_is_a_recorded_failure(self):
        def run(*_args, **_kwargs):
            raise FileNotFoundError("fixture")
        with tempfile.TemporaryDirectory() as directory:
            result = full_test.run_check(full_test.Check("missing", ("fixture",)), Path(directory), run=run)
            log = (Path(directory) / result.log).read_text(encoding="utf-8")
        self.assertEqual(result.status, "failed")
        self.assertIn("FileNotFoundError", log)

    def test_dry_run_does_not_execute_commands(self):
        run = Mock(side_effect=AssertionError("must not execute"))
        with tempfile.TemporaryDirectory() as directory:
            result = full_test.run_check(full_test.Check("planned", ("fixture",)), Path(directory), run=run, dry_run=True)
        self.assertEqual(result.status, "not_run")
        run.assert_not_called()

    def test_upgrade_without_explicit_baselines_is_blocked_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            report, code = full_test.execute_upgrade(
                "yolo-trade", report_dir,
                baseline_macos=None, candidate_macos=None, baseline_ios=None, candidate_ios=None,
            )
            saved = __import__("json").loads((report_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual([item["status"] for item in saved["checks"]], ["blocked", "blocked"])

    def test_upgrade_continues_ios_exercise_after_macos_failure(self):
        calls = []
        def mac_exercise(*_args):
            calls.append("macos")
            raise full_test.UpgradeTestError("fixture failure")
        def ios_exercise(*_args):
            calls.append("ios")
            return {"status": "passed", "data_preserved": True}
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            artifacts = []
            for name in ("old-mac", "new-mac", "old-ios", "new-ios"):
                path = report_dir / name
                path.mkdir()
                (path / "artifact").write_text(name, encoding="utf-8")
                artifacts.append(path)
            report, code = full_test.execute_upgrade(
                "yolo-trade", report_dir,
                baseline_macos=artifacts[0], candidate_macos=artifacts[1],
                baseline_ios=artifacts[2], candidate_ios=artifacts[3],
                mac_exercise=mac_exercise, ios_exercise=ios_exercise,
            )
            self.assertFalse((report_dir / "workspaces/macos").exists())
            self.assertFalse((report_dir / "workspaces/ios").exists())
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["macos", "ios"])
        self.assertEqual([item["status"] for item in report["checks"]], ["failed", "passed"])

    def test_upgrade_rejects_two_copies_of_the_same_artifact(self):
        exercise = Mock(side_effect=AssertionError("must not execute"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            candidate = root / "candidate"
            baseline.mkdir()
            candidate.mkdir()
            (baseline / "same").write_text("same", encoding="utf-8")
            (candidate / "same").write_text("same", encoding="utf-8")
            result = full_test.run_upgrade_check(
                "same-artifact", baseline, candidate, root / "workspaces/same", exercise,
            )
        self.assertEqual(result.status, "blocked")
        exercise.assert_not_called()

    def test_live_phase_blocks_failed_dependencies_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automated = root / "automated.json"
            automated.write_text('{"phase":"automated","status":"failed"}', encoding="utf-8")
            report_dir = root / "report"
            report_dir.mkdir()
            installer = Mock(side_effect=AssertionError("must not install"))
            report, code = full_test.execute_live(
                "yolo-trade", report_dir,
                automated_report=automated, upgrade_report=None,
                mac_candidate=root / "mac", mac_destination=root / "installed",
                mac_backup_root=root / "backups", ios_candidate=root / "ios",
                ios_rollback=root / "rollback", device="private-device",
                state_reader=lambda: {"revision": "fixture", "dirty": False, "difference_fingerprint": "x"},
                mac_installer=installer,
            )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        installer.assert_not_called()

    def test_live_phase_installs_both_apps_without_recording_device_identifier(self):
        from yolo_trade.apple_upgrade import BundleMetadata, IOSReplacement, MacReplacement
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"revision": "fixture", "dirty": False, "difference_fingerprint": "x"}
            mac = root / "mac"
            ios = root / "ios"
            rollback = root / "rollback"
            for path, content in ((mac, "mac"), (ios, "ios"), (rollback, "old-ios")):
                path.mkdir()
                (path / "artifact").write_text(content, encoding="utf-8")
            mac_fingerprint = full_test.tree_fingerprint(mac)
            automated = root / "automated.json"
            automated.write_text(__import__("json").dumps({
                "phase": "automated", "status": "passed", "candidate": state,
                "artifacts": {
                    "macos": {"fingerprint": mac_fingerprint},
                    "ios_device": {"fingerprint": full_test.tree_fingerprint(ios)},
                },
                "checks": [
                    {"name": "xcode-macos", "status": "passed"},
                    {"name": "xcode-ios-device", "status": "passed"},
                ],
            }), encoding="utf-8")
            upgrade = root / "upgrade.json"
            upgrade.write_text(__import__("json").dumps({
                "phase": "upgrade", "status": "passed", "candidate": state,
                "checks": [
                    {"name": "macos-upgrade", "status": "passed", "candidate_fingerprint": mac_fingerprint},
                    {"name": "ios-simulator-upgrade", "status": "passed"},
                ],
            }), encoding="utf-8")
            report_dir = root / "report"
            report_dir.mkdir()
            events = []
            preflight = Mock(side_effect=lambda **_kwargs: events.append("preflight") or {"status": "passed"})
            host_installer = Mock(side_effect=lambda _path: events.append("host"))
            metadata = BundleMetadata("com.yoloagents.yolotrade", "2", "2", "YoloTrade")
            def install_mac(*_args):
                events.append("mac")
                return MacReplacement("installed", "backup", metadata)
            def install_ios(*_args, **_kwargs):
                events.append("ios")
                return IOSReplacement(True, metadata)
            report, code = full_test.execute_live(
                "yolo-trade", report_dir,
                automated_report=automated, upgrade_report=upgrade,
                mac_candidate=mac, mac_destination=root / "installed",
                mac_backup_root=root / "backups", ios_candidate=ios,
                ios_rollback=rollback, device="private-device",
                state_reader=lambda: state, preflight=preflight,
                mac_validator=lambda _path: metadata, ios_validator=lambda _path: metadata,
                mac_installer=install_mac,
                ios_installer=install_ios,
                host_installer=host_installer,
                host_config_reader=lambda: root / "ibkr.json",
            )
            saved = (report_dir / "report.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(preflight.call_count, 3)
        host_installer.assert_called_once()
        self.assertEqual(events, ["preflight", "host", "preflight", "mac", "ios", "preflight"])
        self.assertNotIn("private-device", saved)

    def test_live_phase_restores_mac_and_ios_after_install_failure(self):
        from yolo_trade.apple_upgrade import BundleMetadata, IOSReplacement, MacReplacement
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"revision": "fixture", "dirty": False, "difference_fingerprint": "x"}
            artifacts = {}
            for name in ("mac", "ios", "rollback"):
                path = root / name
                path.mkdir()
                (path / "artifact").write_text(name, encoding="utf-8")
                artifacts[name] = path
            automated = root / "automated.json"
            automated.write_text(__import__("json").dumps({
                "phase": "automated", "status": "passed", "candidate": state,
                "artifacts": {
                    "macos": {"fingerprint": full_test.tree_fingerprint(artifacts["mac"])},
                    "ios_device": {"fingerprint": full_test.tree_fingerprint(artifacts["ios"])},
                },
                "checks": [{"name": "xcode-macos", "status": "passed"},
                           {"name": "xcode-ios-device", "status": "passed"}],
            }), encoding="utf-8")
            upgrade = root / "upgrade.json"
            upgrade.write_text(__import__("json").dumps({
                "phase": "upgrade", "status": "passed", "candidate": state,
                "checks": [
                    {"name": "macos-upgrade", "status": "passed",
                     "candidate_fingerprint": full_test.tree_fingerprint(artifacts["mac"])},
                    {"name": "ios-simulator-upgrade", "status": "passed"},
                ],
            }), encoding="utf-8")
            metadata = BundleMetadata("com.yoloagents.yolotrade", "2", "2", "YoloTrade")
            ios_calls = 0
            def ios_installer(*_args, **_kwargs):
                nonlocal ios_calls
                ios_calls += 1
                if ios_calls == 1:
                    raise RuntimeError("fixture install failure")
                return IOSReplacement(True, metadata)
            restorer = Mock(return_value=MacReplacement("installed", "displaced", metadata))
            host_installer = Mock()
            report_dir = root / "report"
            report_dir.mkdir()
            report, code = full_test.execute_live(
                "yolo-trade", report_dir,
                automated_report=automated, upgrade_report=upgrade,
                mac_candidate=artifacts["mac"], mac_destination=root / "installed",
                mac_backup_root=root / "backups", ios_candidate=artifacts["ios"],
                ios_rollback=artifacts["rollback"], device="private-device",
                state_reader=lambda: state, preflight=lambda **_kwargs: {"status": "passed"},
                mac_validator=lambda _path: metadata, ios_validator=lambda _path: metadata,
                mac_installer=lambda *_args: MacReplacement("installed", str(root / "old.app"), metadata),
                mac_restorer=restorer, ios_installer=ios_installer,
                host_installer=host_installer,
                host_config_reader=lambda: root / "ibkr.json",
            )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(ios_calls, 2)
        restorer.assert_called_once()

    def test_acceptance_runs_independent_checks_and_never_records_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"revision": "fixture", "dirty": False, "difference_fingerprint": "x"}
            live = root / "live.json"
            live.write_text(__import__("json").dumps({
                "phase": "live", "status": "passed", "candidate": state,
            }), encoding="utf-8")
            report_dir = root / "report"
            report_dir.mkdir()
            probe = Mock(return_value={
                "status": "passed", "agent_id": "yolo-trade",
                "provider_authenticated": True, "model_catalog_available": True,
                "mac_client_online": True, "model_roundtrip_passed": True,
                "business_tools_enabled": False, "conversation_saved": False,
                "model": "fixture-model",
            })
            report, code = full_test.execute_acceptance(
                "yolo-trade", report_dir,
                live_report=live, mac_destination=root / "YoloTrade.app",
                device="private-device", ios_client_online=True,
                state_reader=lambda: state,
                preflight=lambda **_kwargs: {"status": "passed"},
                model_probe=probe,
            )
            saved = (report_dir / "report.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "passed")
        self.assertNotIn("private-device", saved)
        probe.assert_called_once()

    def test_acceptance_keeps_model_check_when_ios_ui_evidence_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {"revision": "fixture", "dirty": False, "difference_fingerprint": "x"}
            live = root / "live.json"
            live.write_text(__import__("json").dumps({
                "phase": "live", "status": "passed", "candidate": state,
            }), encoding="utf-8")
            report_dir = root / "report"
            report_dir.mkdir()
            probe = Mock(return_value={"status": "passed"})
            report, code = full_test.execute_acceptance(
                "yolo-trade", report_dir,
                live_report=live, mac_destination=root / "YoloTrade.app",
                device="private-device", ios_client_online=False,
                state_reader=lambda: state,
                preflight=lambda **_kwargs: {"status": "passed"},
                model_probe=probe,
            )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "blocked")
        probe.assert_called_once()

    def test_stored_model_probe_has_a_bounded_outer_timeout(self):
        payload = {
            "status": "passed", "agent_id": "yolo-trade",
            "provider_authenticated": True, "model_catalog_available": True,
            "mac_client_online": True, "model_roundtrip_passed": True,
            "business_tools_enabled": False, "conversation_saved": False,
            "model": "fixture-model",
        }
        run = Mock(return_value=Mock(
            returncode=0, stdout=__import__("json").dumps(payload).encode(), stderr=b"",
        ))
        self.assertEqual(full_test.run_stored_model_probe(run=run)["status"], "passed")
        self.assertEqual(run.call_args.kwargs["timeout"], 75)

    def test_full_test_lock_rejects_a_concurrent_phase_for_same_product(self):
        with full_test.full_test_lock("fixture-product"):
            with self.assertRaises(full_test.FullTestAlreadyRunning):
                with full_test.full_test_lock("fixture-product"):
                    self.fail("nested lock unexpectedly succeeded")


if __name__ == "__main__":
    unittest.main()
