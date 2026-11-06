"""Tests for the whole-PR feature/test pairing gate."""

import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "check_feature_tests.py"
SPEC = importlib.util.spec_from_file_location("check_feature_tests", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
feature_tests = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = feature_tests
SPEC.loader.exec_module(feature_tests)


class FeatureTestPairingTests(unittest.TestCase):
    def missing(self, changed, eligible=None):
        return feature_tests.missing_test_scopes(changed, eligible)

    def test_python_feature_requires_same_scope_test(self):
        changed = ["packages/core-python/yolo_agents/service.py"]
        missing = self.missing(changed)
        self.assertEqual([item.scope.name for item in missing], ["packages/core-python"])

        changed.append("packages/core-python/tests/test_service.py")
        self.assertEqual(self.missing(changed), [])

    def test_other_product_test_cannot_satisfy_agent_change(self):
        changed = [
            "agents/yolo-trade/runtime-python/yolo_trade/paper_orders.py",
            "agents/yolo-beni/runtime-python/tests/test_entrypoint.py",
        ]
        missing = self.missing(changed)
        self.assertEqual([item.scope.name for item in missing], ["agents/yolo-trade/runtime-python"])

    def test_each_changed_scope_needs_its_own_test(self):
        changed = [
            "packages/core-python/yolo_agents/api.py",
            "packages/core-python/tests/test_api.py",
            "apps/yolo-trade/client-swift/Sources/YoloTradeClient/YoloTradeRootView.swift",
        ]
        missing = self.missing(changed)
        self.assertEqual([item.scope.name for item in missing], ["apps/yolo-trade/client-swift"])

    def test_runtime_json_is_behavior_and_requires_agent_test(self):
        changed = ["agents/yolo-trade/runtime-python/yolo_trade/risk-policy.json"]
        self.assertEqual(
            [item.scope.name for item in self.missing(changed)],
            ["agents/yolo-trade/runtime-python"],
        )

    def test_platform_entry_behavior_uses_product_client_tests(self):
        changed = [
            "apps/yolo-beni/ios/Sources/YoloBeniIOS/YoloBeniIOSApp.swift",
            "apps/yolo-beni/client-swift/Tests/YoloBeniClientTests/YoloBeniProductTests.swift",
        ]
        self.assertEqual(self.missing(changed), [])

    def test_deleted_test_does_not_count_as_coverage(self):
        changed = [
            "bin/yolo_agents_full_test.py",
            "bin/tests/test_yolo_agents_full_test.py",
        ]
        eligible = ["bin/yolo_agents_full_test.py"]
        missing = self.missing(changed, eligible)
        self.assertEqual([item.scope.name for item in missing], ["repository command tooling"])

    def test_test_directory_documentation_is_not_test_code(self):
        self.assertEqual(len(self.missing(["bin/command.py", "bin/tests/README.md"])), 1)

    def test_docs_resources_and_existing_tests_do_not_trigger_gate(self):
        changed = [
            "docs/ROADMAP.md",
            "apps/yolo-trade/xcode/Resources/Assets.xcassets/Contents.json",
            "agents/yolo-trade/runtime-python/tests/test_risk_policy.py",
        ]
        self.assertEqual(self.missing(changed), [])


class PRDiffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-b", "master")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Gate Test")
        self.write("README.md", "baseline\n")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True).stdout.decode().strip()

    def write(self, path, content):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def missing_at(self, head):
        return feature_tests.missing_test_scopes(
            feature_tests.pr_paths(self.base, head, "ACDMT", root=self.root),
            feature_tests.pr_paths(self.base, head, "ACMT", root=self.root),
        )

    def test_pairing_across_commits_ignores_staged_unrelated_changes(self):
        self.write("packages/core-python/yolo_agents/service.py", "value = 1\n")
        intermediate = self.commit()
        self.assertEqual(len(self.missing_at(intermediate)), 1)
        self.write("packages/core-python/tests/test_service.py", "assert True\n")
        head = self.commit()
        self.write("agents/yolo-beni/runtime-python/yolo_beni/new.py", "value = 1\n")
        self.git("add", "--all")
        self.assertEqual(self.missing_at(head), [])

    def test_staged_test_cannot_satisfy_missing_pr_test(self):
        self.write("bin/new_command.py", "print('behavior')\n")
        head = self.commit()
        self.write("bin/tests/test_new_command.py", "assert True\n")
        self.git("add", "--all")
        self.assertEqual(len(self.missing_at(head)), 1)

    def test_deleted_test_and_deleted_production_remain_conservative(self):
        self.write("bin/command.py", "print('baseline')\n")
        self.write("bin/tests/test_command.py", "assert True\n")
        self.base = self.commit()
        (self.root / "bin/tests/test_command.py").unlink()
        (self.root / "bin/command.py").unlink()
        head = self.commit()
        self.assertEqual([item.scope.name for item in self.missing_at(head)], ["repository command tooling"])

    def test_cross_scope_rename_requires_tests_for_both_scopes(self):
        source = "packages/core-python/yolo_agents/service.py"
        destination = "agents/yolo-trade/runtime-python/yolo_trade/service.py"
        self.write(source, "value = 1\n")
        self.base = self.commit()
        self.write(destination, (self.root / source).read_text())
        (self.root / source).unlink()
        self.write("agents/yolo-trade/runtime-python/tests/test_service.py", "assert True\n")
        head = self.commit()
        self.assertEqual([item.scope.name for item in self.missing_at(head)], ["packages/core-python"])

    def test_test_renamed_out_of_scope_does_not_satisfy_it(self):
        test = "packages/core-python/tests/test_old.py"
        self.write(test, "assert True\n")
        self.base = self.commit()
        self.write("packages/core-python/yolo_agents/service.py", "value = 1\n")
        self.write("bin/tests/test_old.py", (self.root / test).read_text())
        (self.root / test).unlink()
        self.assertEqual(len(self.missing_at(self.commit())), 1)

    def test_pr_diff_excludes_base_only_commits_after_fork(self):
        self.git("checkout", "-b", "feat/example")
        self.write("docs/change.md", "branch change\n")
        head = self.commit()
        self.git("checkout", "master")
        self.write("packages/core-python/yolo_agents/upstream.py", "value = 1\n")
        self.base = self.commit()
        self.assertEqual(self.missing_at(head), [])

    def test_missing_arguments_and_non_commit_shas_fail_closed(self):
        with self.assertRaises(SystemExit) as stopped:
            feature_tests.main([])
        self.assertEqual(stopped.exception.code, 2)
        for invalid in ("master", self.base[:7], "f" * 40):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                feature_tests.pr_paths(invalid, self.base, "ACMT", root=self.root)


if __name__ == "__main__":
    unittest.main()
