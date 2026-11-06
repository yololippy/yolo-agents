from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_yolo_trade_ios_simulator as simulator_test


class HostedSimulatorSDKTests(unittest.TestCase):
    def test_runs_hosted_arm64_tests_and_always_deletes_device(self):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            stdout = b"private-device\n" if "create" in command else b""
            return Mock(returncode=0, stdout=stdout, stderr=b"")

        with tempfile.TemporaryDirectory() as directory:
            result = simulator_test.run_hosted_sdk_tests(
                Path(directory) / "derived",
                root=Path(directory), run=run,
                selector=lambda **_kwargs: ("runtime", "device-type"),
            )

        xcodebuild = next(command for command in calls if command[0].endswith("xcodebuild"))
        self.assertIn("YoloTradeIOS", xcodebuild)
        self.assertIn("ARCHS=arm64", xcodebuild)
        self.assertIn("ONLY_ACTIVE_ARCH=YES", xcodebuild)
        self.assertEqual(calls[-2][-2:], ["shutdown", "private-device"])
        self.assertEqual(calls[-1][-2:], ["delete", "private-device"])
        self.assertFalse(result["device_identifier_recorded"])

    def test_failed_xcode_test_still_deletes_device(self):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            stdout = b"private-device\n" if "create" in command else b""
            code = 65 if command[0].endswith("xcodebuild") else 0
            return Mock(returncode=code, stdout=stdout, stderr=b"")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(simulator_test.SimulatorSDKTestError):
                simulator_test.run_hosted_sdk_tests(
                    Path(directory) / "derived",
                    root=Path(directory), run=run,
                    selector=lambda **_kwargs: ("runtime", "device-type"),
                )

        self.assertEqual(calls[-2][-2:], ["shutdown", "private-device"])
        self.assertEqual(calls[-1][-2:], ["delete", "private-device"])


if __name__ == "__main__":
    unittest.main()
