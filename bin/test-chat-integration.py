#!/usr/bin/env python3
"""Test the native Swift SDK against a disposable Python demo service.

No packages are installed, no cloud model is called, and no real credentials or
application databases are used. --sdk-only avoids SwiftUI SDK requirements by
linking the same SDK/test source files into a temporary, dependency-free package.
"""

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading


def simulator_test_configuration(source, destination, environment):
    """Copy build products' test configuration without moving their path base."""
    original_root = str(source.resolve().parent)
    configuration = plistlib.loads(source.read_bytes())
    targets = 0

    def configure(value):
        nonlocal targets
        if isinstance(value, str):
            return value.replace("__TESTROOT__", original_root)
        if isinstance(value, list):
            return [configure(item) for item in value]
        if isinstance(value, dict):
            result = {key: configure(item) for key, item in value.items()}
            if "TestBundlePath" in result:
                result.setdefault("EnvironmentVariables", {}).update(environment)
                targets += 1
            return result
        return value

    configured = configure(configuration)
    if targets == 0:
        raise ValueError("No test targets found in the supplied xctestrun configuration")
    destination.write_bytes(plistlib.dumps(configured))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-only", action="store_true")
    parser.add_argument("--probe", action="store_true", help="run the SDK executable probe without any test framework")
    parser.add_argument("--testing-plugin", type=Path, help="installed TestingMacros library when CLT cannot discover it automatically")
    parser.add_argument("--ios-simulator", help="booted simulator UUID for an iOS test-without-building run")
    parser.add_argument("--xctestrun", type=Path, help="existing build-for-testing configuration; required with --ios-simulator")
    args = parser.parse_args()
    if bool(args.ios_simulator) != bool(args.xctestrun):
        parser.error("--ios-simulator and --xctestrun must be provided together")
    if args.ios_simulator and (args.probe or args.sdk_only or args.testing_plugin):
        parser.error("iOS testing cannot be combined with Swift package test options")
    if args.xctestrun and not args.xctestrun.is_file():
        parser.error("--xctestrun must point to an existing build-for-testing configuration")
    root = Path(__file__).resolve().parents[1]
    swift = shutil.which("swift")
    if swift is None:
        parser.error("Swift tools are required; install/enable Xcode or Command Line Tools first")
    if args.testing_plugin and not args.testing_plugin.is_file():
        parser.error("--testing-plugin must point to an existing installed library")
    sys.path.insert(0, str(root / "server"))
    from trading_agent.agent_runtime import create_runtime
    from trading_agent.api import ChatHTTPServer
    from trading_agent.auth import DeviceAuth
    from trading_agent.service import ChatService
    from trading_agent.store import Store
    with tempfile.TemporaryDirectory(prefix="trading-agent-integration-") as temp:
        folder = Path(temp)
        store = Store(folder / "app.sqlite3")
        runtime = create_runtime("demo", None, folder, store)
        service = ChatService(store, runtime)
        auth = DeviceAuth(folder / "devices.sqlite3")
        server = ChatHTTPServer(("127.0.0.1", 0), service, auth)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        package = root / "apple"
        if args.sdk_only or args.probe:
            package = folder / "swift-sdk"
            package.mkdir()
            (package / "Sources").mkdir()
            (package / "Sources/TradingAgentSDK").symlink_to(root / "apple/Sources/TradingAgentSDK", target_is_directory=True)
            if args.probe:
                (package / "Sources/TradingAgentProbe").mkdir()
                (package / "Sources/TradingAgentProbe/Probe.swift").symlink_to(root / "apple/Sources/TradingAgentProbe/Probe.swift")
                second_target = '.executableTarget(name: "TradingAgentProbe", dependencies: ["TradingAgentSDK"]),'
            else:
                (package / "Tests").mkdir()
                (package / "Tests/TradingAgentSDKTests").symlink_to(root / "apple/Tests/TradingAgentSDKTests", target_is_directory=True)
                second_target = '.testTarget(name: "TradingAgentSDKTests", dependencies: ["TradingAgentSDK"]),'
            (package / "Package.swift").write_text('''// swift-tools-version: 6.0
import PackageDescription
let package = Package(name: "TradingAgentIntegration", platforms: [.macOS(.v14)], targets: [
    .target(name: "TradingAgentSDK"),
    ''' + second_target + '''
])
''')
        test_environment = dict(
            TRADING_AGENT_TEST_URL=f"http://127.0.0.1:{server.server_port}",
            TRADING_AGENT_PAIRING_CODE=auth.issue_pairing_code("ios" if args.ios_simulator else "macos"),
            TRADING_AGENT_TEST_PLATFORM="ios" if args.ios_simulator else "macos")
        env = dict(os.environ, **test_environment)
        print("Testing the Swift SDK against a disposable loopback demo service; no cloud calls.", flush=True)
        try:
            if args.ios_simulator:
                configuration = folder / "integration.xctestrun"
                simulator_test_configuration(args.xctestrun, configuration, test_environment)
                command = ["xcodebuild", "test-without-building", "-xctestrun", str(configuration),
                           "-destination", f"platform=iOS Simulator,id={args.ios_simulator}",
                           "-parallel-testing-enabled", "NO", "-maximum-concurrent-test-simulator-destinations", "1"]
            elif args.probe:
                command = [swift, "run", "--build-system", "native", "--package-path", str(package), "TradingAgentProbe"]
            else:
                command = [swift, "test", "--package-path", str(package), "--filter", "liveServiceRoundTrip"]
                if args.testing_plugin:
                    command += ["-Xswiftc", "-load-plugin-library", "-Xswiftc", str(args.testing_plugin)]
            result = subprocess.run(command, env=env)
            if result.returncode == 0:
                completed = any(
                    message["role"] == "assistant" and message["status"] == "completed"
                    for conversation in store.list_conversations(include_archived=True)
                    for message in store.list_messages(conversation["id"]))
                if not completed:
                    print("FAIL: no completed reply reached the disposable service; the live test may have been skipped.", file=sys.stderr)
                    return 1
            return result.returncode
        finally:
            service.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            runtime.close()
            store.close()


if __name__ == "__main__":
    sys.exit(main())
