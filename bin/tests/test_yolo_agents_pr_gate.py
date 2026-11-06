"""Semantic gate tests use disposable Git repositories and real child processes.

No Xcode, production Host, provider account, network, or broker is used here.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("yolo_agents_pr_gate", Path(__file__).resolve().parents[1] / "yolo_agents_pr_gate.py")
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.root = self.root.resolve()
        self.git("init", "-b", "master")
        self.git("config", "user.name", "PR Gate Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("remote", "add", "origin", "git@github.com:example/yolo-agents.git")
        self.write(".gitignore", ".venv/\n")
        self.write("README.md", "baseline\n")
        self.base = self.commit()
        self.git("checkout", "-b", "feat/example")
        self.write("docs/change.md", "candidate\n")
        self.head = self.commit()
        self.git("update-ref", "refs/remotes/origin/master", self.base)
        self.git("update-ref", "refs/pull/3/head", self.head)
        python = self.root / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.meta = {"repository": "example/yolo-agents", "pr_number": 3, "base_ref": "master", "base_sha": self.base,
                     "head_ref": "feat/example", "head_sha": self.head, "state": "open", "draft": False}
        self.meta_path = Path(self.temporary.name) / "metadata.json"
        self.export_metadata()
        self.run_dir = None

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True).stdout.decode().strip()

    def write(self, path, text):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def export_metadata(self, **updates):
        self.meta_path.write_text(json.dumps(dict(self.meta, **updates)))

    def prepare(self):
        prepared = gate.prepare(self.root, self.meta_path, "yolo-trade")
        self.run_dir = Path(prepared["run_dir"])
        return gate.load_manifest(self.run_dir, self.root)

    @staticmethod
    def small_plan(group, manifest, run_dir):
        return [gate.Check(group + "-smoke", (sys.executable, "-c", "print('executed')"), 10)]

    def passing_groups(self):
        manifest, key = self.prepare()
        for group in gate.GROUPS:
            with patch.object(gate, "checks_for", side_effect=self.small_plan):
                report = gate.run_group(self.root, self.run_dir, group)
            self.assertEqual(report["status"], "passed")
        return manifest, key

    def ui_report(self, manifest, **updates):
        folder = self.run_dir / "ui"
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / "report.json"
        report = dict(gate.identity(manifest), group="ui", status="passed", checks=[
            {"name": name, "status": "passed"} for name in ("mac_model_reply", "simulator_model_reply", "bidirectional_sync")
        ])
        report.update(updates)
        path.write_text(json.dumps(report))
        return path

    def summarize(self, ui):
        # Represents main's new official connector export after all checks.
        self.export_metadata()
        with patch.object(gate, "checks_for", side_effect=self.small_plan):
            return gate.summary(self.root, self.run_dir, self.meta_path, ui)

    def test_prepare_freezes_all_three_detached_worktrees_and_keeps_source(self):
        initial = self.git("status", "--porcelain")
        manifest, _ = self.prepare()
        self.assertEqual(set(manifest["groups"]), {"p0", "mac", "simulator"})
        self.assertEqual(manifest["python"], str(self.root / ".venv/bin/python"))
        self.assertEqual(self.git("symbolic-ref", "HEAD"), "refs/heads/feat/example")
        self.assertEqual(self.git("status", "--porcelain"), initial)
        self.assertEqual(self.git("rev-parse", f"refs/yolo-pr-gate/{manifest['run_id']}/head"), self.head)
        for group, path in manifest["groups"].items():
            self.assertEqual(gate.git(Path(path), "rev-parse", "HEAD"), self.head)
            detached = subprocess.run(["git", "symbolic-ref", "HEAD"], cwd=path, capture_output=True)
            self.assertNotEqual(detached.returncode, 0)
            self.assertEqual((self.run_dir / group).stat().st_mode & 0o077, 0)

    def test_metadata_and_origin_fail_closed(self):
        variants = ({"state": "closed"}, {"draft": True}, {"base_ref": "main"}, {"head_ref": "topic/foo"},
                    {"repository": "other/repo"}, {"head_sha": self.head[:7]}, {"pr_number": True})
        for variant in variants:
            with self.subTest(variant=variant):
                self.export_metadata(**variant)
                with self.assertRaises(gate.GateError):
                    gate.prepare(self.root, self.meta_path, "yolo-trade")
        self.export_metadata()
        self.git("remote", "set-url", "origin", "https://github.com/example/yolo-agents.git")
        with self.assertRaises(gate.GateError):
            gate.prepare(self.root, self.meta_path, "yolo-trade")

    def test_unsupported_product_does_not_create_trade_run(self):
        with self.assertRaisesRegex(gate.GateError, "产品"):
            gate.prepare(self.root, self.meta_path, "yolo-beni")
        self.assertFalse((self.root / ".git/yolo-agents-pr-gate").exists())

    def test_dirty_source_and_stale_base_or_head_are_rejected(self):
        self.write("README.md", "dirty\n")
        with self.assertRaises(gate.GateError):
            gate.prepare(self.root, self.meta_path, "yolo-trade")
        self.write("README.md", "baseline\n")
        for ref, wrong, correct in (("refs/remotes/origin/master", self.head, self.base), ("refs/pull/3/head", self.base, self.head)):
            self.git("update-ref", ref, wrong)
            with self.subTest(ref=ref), self.assertRaises(gate.GateError):
                gate.prepare(self.root, self.meta_path, "yolo-trade")
            self.git("update-ref", ref, correct)

    def test_missing_primary_venv_blocks_without_install(self):
        (self.root / ".venv/bin/python").unlink()
        with self.assertRaisesRegex(gate.GateError, "venv"):
            gate.prepare(self.root, self.meta_path, "yolo-trade")

    def test_failure_continues_independent_checks_and_timeout(self):
        self.prepare()
        plan = [gate.Check("fail", (sys.executable, "-c", "raise SystemExit(2)"), 10),
                gate.Check("timeout", (sys.executable, "-c", "import time; time.sleep(60)"), 0.1),
                gate.Check("after", (sys.executable, "-c", "print('still executed')"), 10)]
        with patch.object(gate, "checks_for", return_value=plan):
            report = gate.run_group(self.root, self.run_dir, "p0")
        self.assertEqual(report["status"], "failed")
        self.assertEqual([item["status"] for item in report["checks"]], ["failed", "failed", "passed"])
        self.assertEqual(report["checks"][1]["reason"], "timeout")
        self.assertIn("still executed", (self.run_dir / report["checks"][2]["log"]).read_text())

    def test_controller_interrupt_stops_only_the_active_check_process_group(self):
        output = Path(self.temporary.name) / "output"
        (output / "logs").mkdir(parents=True)
        process = Mock(pid=123456)
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        check = gate.Check("owned", (sys.executable, "-c", "pass"), 10)
        with patch.object(gate.subprocess, "Popen", return_value=process), patch.object(gate.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                gate.execute_check(check, self.root, output)
        kill.assert_called_once_with(process.pid, gate.signal.SIGINT)
        self.assertEqual(process.wait.call_count, 2)

    def test_dry_run_is_not_a_passing_report(self):
        manifest, key = self.prepare()
        with patch.object(gate, "checks_for", side_effect=self.small_plan):
            report = gate.run_group(self.root, self.run_dir, "p0", dry_run=True)
            self.assertEqual(report["status"], "not_run")
            self.assertEqual(report["checks"][0]["exit_code"], None)
            with self.assertRaises(gate.GateError):
                gate.validate_group(report, "p0", manifest, self.run_dir, key)

    def test_common_directory_lock_excludes_other_linked_worktree(self):
        manifest, _ = self.prepare()
        second = Path(manifest["groups"]["mac"])
        self.assertEqual(gate.common_dir(second), gate.common_dir(self.root))
        resource = gate.common_dir(self.root) / "yolo-agents-pr-gate/locks/simulator.lock"
        with gate.lock(resource):
            with self.assertRaisesRegex(gate.GateError, "占用"):
                gate.run_group(second, self.run_dir, "simulator")
        self.assertFalse((self.run_dir / "simulator/report.json").exists())

    def test_simulator_respects_shared_product_ui_and_full_test_lock(self):
        manifest, _ = self.prepare()
        resource = Path(manifest["common_dir"]) / "yolo-agents-pr-gate/locks/yolo-trade-ui.lock"
        with gate.lock(resource):
            with self.assertRaises(gate.GateError):
                gate.run_group(self.root, self.run_dir, "simulator")
        self.assertFalse((self.run_dir / "simulator/report.json").exists())

    def test_read_only_runner_blocks_polluted_worktree(self):
        manifest, _ = self.prepare()
        (Path(manifest["groups"]["p0"]) / "untracked.txt").write_text("unexpected\n")
        report = gate.run_group(self.root, self.run_dir, "p0")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["checks"], [])

    def test_command_that_changes_tracked_source_cannot_pass(self):
        self.prepare()
        plan = [gate.Check("changes-source", (sys.executable, "-c", "from pathlib import Path; Path('README.md').write_text('changed')"), 10)]
        with patch.object(gate, "checks_for", return_value=plan):
            report = gate.run_group(self.root, self.run_dir, "p0")
        self.assertEqual(report["checks"][0]["status"], "passed")
        self.assertEqual(report["status"], "blocked")
        self.assertNotIn("after_clean", report)

    def test_cleanup_does_not_remove_worktree_with_running_group_lock(self):
        manifest, _ = self.prepare()
        resource = Path(manifest["common_dir"]) / f"yolo-agents-pr-gate/locks/{manifest['run_id']}-p0.lock"
        with gate.lock(resource):
            with self.assertRaises(gate.GateError):
                gate.cleanup(self.root, self.run_dir)
        self.assertTrue(Path(manifest["groups"]["p0"]).is_dir())

    def test_successful_summary_needs_ui_and_refreshed_official_metadata(self):
        manifest, _ = self.passing_groups()
        ui = self.ui_report(manifest)
        result = self.summarize(ui)
        self.assertTrue(result["merge_ready"])
        self.assertEqual(result["status"], "passed")
        stored = gate.read_json(self.run_dir / "summary.json")
        self.assertEqual(stored["head_sha"], self.head)

    def test_missing_group_or_ui_report_never_ready(self):
        manifest, _ = self.passing_groups()
        ui = self.ui_report(manifest)
        (self.run_dir / "mac/report.json").unlink()
        self.assertFalse(self.summarize(ui)["merge_ready"])
        self.assertFalse(self.summarize(self.run_dir / "ui/missing.json")["merge_ready"])

    def test_report_tampering_and_bound_identity_are_rejected(self):
        manifest, key = self.passing_groups()
        original = gate.read_json(self.run_dir / "p0/report.json")
        modified = dict(original, status="passed", product="yolo-beni")
        with self.assertRaisesRegex(gate.GateError, "收据"):
            gate.validate_group(modified, "p0", manifest, self.run_dir, key)
        variants = ({"run_id": "a" * 32}, {"base_sha": self.head}, {"head_sha": self.base},
                    {"group": "mac"}, {"schema_version": 2}, {"status": "blocked"}, {"dry_run": True})
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(gate.GateError), patch.object(gate, "checks_for", side_effect=self.small_plan):
                gate.validate_group(gate.signed(dict(original, **variant), key), "p0", manifest, self.run_dir, key)

    def test_duplicate_or_missing_checks_and_forged_logs_cannot_pass(self):
        manifest, key = self.passing_groups()
        original = gate.read_json(self.run_dir / "p0/report.json")
        for checks in ([], original["checks"] * 2, [dict(original["checks"][0], exit_code=1)]):
            with self.subTest(checks=checks), self.assertRaises(gate.GateError), patch.object(gate, "checks_for", side_effect=self.small_plan):
                gate.validate_group(gate.signed(dict(original, checks=checks), key), "p0", manifest, self.run_dir, key)
        log = self.run_dir / original["checks"][0]["log"]
        log.write_text("changed after checking\n")
        with self.assertRaises(gate.GateError), patch.object(gate, "checks_for", side_effect=self.small_plan):
            gate.validate_group(original, "p0", manifest, self.run_dir, key)

    def test_probe_dryrun_and_mismatched_ui_report_do_not_pass(self):
        manifest, _ = self.passing_groups()
        original_path = self.ui_report(manifest)
        original = json.loads(original_path.read_text())
        variants = ({"group": "acceptance"}, {"dry_run": True}, {"production_probe": True}, {"head_sha": self.base},
                    {"product": "yolo-beni"}, {"checks": original["checks"][:1]}, {"checks": original["checks"][:1] * 3},
                    {"checks": [{"name": "model_probe", "status": "passed"}]})
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(gate.GateError):
                gate.validate_ui(dict(original, **variant), manifest)

    def test_refreshed_metadata_changed_head_or_base_blocks_even_with_old_local_refs(self):
        manifest, _ = self.passing_groups()
        ui = self.ui_report(manifest)
        for updates in ({"head_sha": self.base}, {"base_sha": self.head}):
            self.export_metadata(**updates)
            with patch.object(gate, "checks_for", side_effect=self.small_plan):
                self.assertFalse(gate.summary(self.root, self.run_dir, self.meta_path, ui)["merge_ready"])

    def test_stale_metadata_and_worktree_changes_after_checks_block(self):
        manifest, _ = self.passing_groups()
        ui = self.ui_report(manifest)
        stale = time.time() - 121
        os.utime(self.meta_path, (stale, stale))
        with patch.object(gate, "checks_for", side_effect=self.small_plan):
            self.assertFalse(gate.summary(self.root, self.run_dir, self.meta_path, ui)["merge_ready"])
        (Path(manifest["groups"]["mac"]) / "README.md").write_text("polluted\n")
        self.assertFalse(self.summarize(ui)["merge_ready"])

    def test_local_base_update_during_conflict_check_is_detected_before_ready(self):
        manifest, _ = self.passing_groups()
        ui = self.ui_report(manifest)
        def concurrent_update(*_args):
            self.git("update-ref", "refs/remotes/origin/master", self.head)
            return True
        with patch.object(gate, "conflict_free", side_effect=concurrent_update):
            self.assertFalse(self.summarize(ui)["merge_ready"])

    def test_frozen_ref_modification_is_rejected(self):
        manifest, _ = self.prepare()
        self.git("update-ref", f"refs/yolo-pr-gate/{manifest['run_id']}/head", self.base)
        with self.assertRaisesRegex(gate.GateError, "冻结"):
            gate.load_manifest(self.run_dir, self.root)

    def test_cleanup_preserves_reports_and_refuses_dirty_worktree(self):
        manifest, _ = self.prepare()
        dirty = Path(manifest["groups"]["p0"]) / "new.txt"
        dirty.write_text("preserve\n")
        with self.assertRaises(gate.GateError):
            gate.cleanup(self.root, self.run_dir)
        self.assertTrue(dirty.is_file())
        dirty.unlink()
        result = gate.cleanup(self.root, self.run_dir)
        self.assertEqual(set(result["removed"]), set(gate.GROUPS))
        self.assertTrue((self.run_dir / "manifest.json").is_file())
        self.assertTrue((self.root / "README.md").is_file())

    def test_cleanup_removes_build_artifacts_and_retains_reports_and_logs(self):
        self.prepare()
        for group in gate.GROUPS:
            (self.run_dir / group / "build/artifact.bin").write_bytes(b"build output")
            (self.run_dir / group / "logs/check.log").write_text("retained log\n")
            (self.run_dir / group / "report.json").write_text('{"retained":true}\n')

        result = gate.cleanup(self.root, self.run_dir)

        self.assertEqual(set(result["build_artifacts_removed"]), set(gate.GROUPS))
        for group in gate.GROUPS:
            self.assertFalse((self.run_dir / group / "build").exists())
            self.assertEqual((self.run_dir / group / "logs/check.log").read_text(), "retained log\n")
            self.assertEqual((self.run_dir / group / "report.json").read_text(), '{"retained":true}\n')
        self.assertEqual(gate.cleanup(self.root, self.run_dir)["build_artifacts_removed"], [])

    def test_cleanup_rejects_build_symlink_without_touching_external_target(self):
        manifest, _ = self.prepare()
        build = self.run_dir / "mac/build"
        build.rmdir()
        external = Path(self.temporary.name) / "external-build"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("keep\n")
        build.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(gate.GateError, "构建目录无效"):
            gate.cleanup(self.root, self.run_dir)

        self.assertTrue(marker.is_file())
        self.assertTrue(Path(manifest["groups"]["p0"]).is_dir())

    def test_merge_conflicts_block_readiness(self):
        # Divergent changes to the same file: keep the source at PR head while
        # exporting and fetching the newer base before prepare.
        self.write("README.md", "head version\n")
        self.head = self.commit()
        self.git("checkout", "master")
        self.write("README.md", "base version\n")
        self.base = self.commit()
        self.git("checkout", "feat/example")
        self.git("update-ref", "refs/remotes/origin/master", self.base)
        self.git("update-ref", "refs/pull/3/head", self.head)
        self.meta.update(base_sha=self.base, head_sha=self.head)
        self.export_metadata()
        manifest, _ = self.passing_groups()
        self.assertFalse(self.summarize(self.ui_report(manifest))["merge_ready"])

    def test_production_plans_cover_target_product_without_other_product_builds(self):
        manifest, _ = self.prepare()
        names = set()
        for group in gate.GROUPS:
            plan = gate.checks_for(group, manifest, self.run_dir)
            names.update(item.name for item in plan)
            for check in plan:
                self.assertNotIn("yolo-beni", " ".join(check.command))
        self.assertTrue({"feature-test-pairing", "repository-scripts", "python-core", "python-product", "agent-isolation", "local-integration",
                         "swift-sdk", "swift-ui", "swift-product", "xcode-macos", "xcode-ios-simulator", "ios-hosted-sdk"}.issubset(names))

    def test_json_duplicate_fields_are_rejected(self):
        self.meta_path.write_text('{"state":"open","state":"closed"}')
        with self.assertRaisesRegex(gate.GateError, "重复"):
            gate.metadata(self.meta_path)


if __name__ == "__main__":
    unittest.main()
