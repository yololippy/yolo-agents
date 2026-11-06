from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_yolo_trade_ios_simulator as simulator_test


class HostedSimulatorSDKTests(unittest.TestCase):
    def test_acceptance_probe_is_signed_with_installed_mac_identity_before_keychain_access(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "bin/YoloTradeAcceptanceProbe"
            executable.parent.mkdir()
            executable.write_bytes(b"fixture")
            app = root / "YoloTrade.app"

            def run(command, **_kwargs):
                calls.append(command)
                if "--show-bin-path" in command:
                    return Mock(returncode=0, stdout=str(executable.parent).encode() + b"\n", stderr=b"")
                if command[:3] == ["/usr/bin/codesign", "-d", "--verbose=2"]:
                    return Mock(returncode=0, stdout=b"", stderr=(
                        b"Identifier=com.yoloagents.yolotrade\n"
                        b"Authority=Apple Development: Fixture (TEAM)\n"
                    ))
                if command[:4] == ["/usr/bin/security", "find-identity", "-v", "-p"]:
                    return Mock(returncode=0, stdout=(
                        b'  1) 0123456789ABCDEF0123456789ABCDEF01234567 '
                        b'"Apple Development: Fixture (TEAM)"\n'
                    ), stderr=b"")
                if command[0] == str(executable):
                    return Mock(returncode=0, stdout=b'{"status":"passed"}', stderr=b"")
                return Mock(returncode=0, stdout=b"", stderr=b"")

            output = simulator_test.run_signed_acceptance_probe(
                ["--endpoint", "http://127.0.0.1:8765"],
                root=root, mac_app=app, run=run,
            )

        self.assertEqual(output, b'{"status":"passed"}')
        sign_index = next(index for index, command in enumerate(calls)
                          if command[:2] == ["/usr/bin/codesign", "--force"])
        execute_index = next(index for index, command in enumerate(calls)
                             if command[0] == str(executable))
        self.assertLess(sign_index, execute_index)
        self.assertIn("com.yoloagents.yolotrade", calls[sign_index])

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
        self.assertFalse(result["production_model_roundtrip"])

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

    def test_production_probe_injects_short_lived_session_only_into_temporary_configuration(self):
        calls = []
        injected = []

        with tempfile.TemporaryDirectory() as directory:
            derived = Path(directory) / "derived"
            products = derived / "Build/Products"
            products.mkdir(parents=True)
            configuration = products / "fixture.xctestrun"
            configuration.write_bytes(plistlib.dumps({
                "TestTarget": {
                    "TestBundlePath": "__TESTROOT__/Fixture.xctest",
                    "EnvironmentVariables": {"EXISTING": "1"},
                },
            }))

            def run(command, **_kwargs):
                calls.append(command)
                if "test-without-building" in command:
                    path = Path(command[command.index("-xctestrun") + 1])
                    value = plistlib.loads(path.read_bytes())
                    injected.append(value["TestTarget"]["EnvironmentVariables"])
                stdout = b"private-device\n" if "create" in command else b""
                return Mock(returncode=0, stdout=stdout, stderr=b"")

            result = simulator_test.run_hosted_sdk_tests(
                derived,
                root=Path(directory), run=run,
                selector=lambda **_kwargs: ("runtime", "device-type"),
                production_probe=True,
                host_endpoint="http://127.0.0.1:8765",
                mac_app=Path(directory) / "YoloTrade.app",
                session_issuer=lambda **_kwargs: {
                    "endpoint": "https://example.test",
                    "pairing_code": "private-pairing-code",
                    "grant": "private-probe-grant",
                    "expires_in": 300,
                    "allow_insecure_local_http": False,
                },
            )

        self.assertTrue(result["production_model_roundtrip"])
        self.assertEqual(injected, [{
            "EXISTING": "1",
            "YOLO_TRADE_SIMULATOR_PROBE_ENDPOINT": "https://example.test",
            "YOLO_TRADE_SIMULATOR_PAIRING_CODE": "private-pairing-code",
            "YOLO_TRADE_SIMULATOR_PROBE_GRANT": "private-probe-grant",
            "YOLO_TRADE_SIMULATOR_ALLOW_INSECURE_LOCAL_HTTP": "0",
        }])
        self.assertEqual(calls[-2][-2:], ["shutdown", "private-device"])
        self.assertEqual(calls[-1][-2:], ["delete", "private-device"])


if __name__ == "__main__":
    unittest.main()
