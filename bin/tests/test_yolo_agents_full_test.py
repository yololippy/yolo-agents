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
        ])
        command_text = "\n".join(" ".join(item.command) for item in checks)
        self.assertIn("agents/yolo-trade", command_text)
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


if __name__ == "__main__":
    unittest.main()
