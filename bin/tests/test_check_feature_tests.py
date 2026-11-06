"""Tests for the staged feature/test pairing gate."""

import importlib.util
from pathlib import Path
import sys
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

    def test_docs_resources_and_existing_tests_do_not_trigger_gate(self):
        changed = [
            "docs/ROADMAP.md",
            "apps/yolo-trade/xcode/Resources/Assets.xcassets/Contents.json",
            "agents/yolo-trade/runtime-python/tests/test_risk_policy.py",
        ]
        self.assertEqual(self.missing(changed), [])


if __name__ == "__main__":
    unittest.main()
