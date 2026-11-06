from pathlib import Path
import plistlib
import shutil
import tempfile
import unittest
from unittest.mock import Mock

from yolo_trade.apple_upgrade import (
    AppleUpgradeError, bundle_metadata, replace_ios_app, replace_macos_app, restore_macos_app,
)


def app_bundle(root: Path, name: str, *, bundle_id: str = "com.yoloagents.yolotrade",
               version: str = "1", extra: bool = False, platform: str = "macos") -> Path:
    app = root / name
    executable = app / ("Contents/MacOS/YoloTradeMac" if platform == "macos" else "YoloTradeIOS")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    executable.chmod(0o755)
    plist = app / ("Contents/Info.plist" if platform == "macos" else "Info.plist")
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": "YoloTradeMac" if platform == "macos" else "YoloTradeIOS",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
    }))
    if extra:
        (app / "Contents/Resources").mkdir()
        (app / "Contents/Resources/stale.txt").write_text("old", encoding="utf-8")
    return app


def apple_runner(command, **_kwargs):
    if command[0] == "/usr/bin/ditto":
        shutil.copytree(command[-2], command[-1], copy_function=shutil.copy2)
    return Mock(returncode=0)


class DeviceRunner:
    def __init__(self, apps, *, install_fails=False, report_installed_version=True):
        self.apps = dict(apps)
        self.install_fails = install_fails
        self.report_installed_version = report_installed_version
        self.installs = 0

    def __call__(self, command, **_kwargs):
        if command[0] == "/usr/bin/codesign":
            return Mock(returncode=0)
        output = Path(command[command.index("--json-output") + 1])
        if "info" in command:
            rows = [
                {"bundleIdentifier": bundle_id, "version": versions[0], "bundleVersion": versions[1]}
                for bundle_id, versions in self.apps.items()
            ]
            output.write_text(__import__("json").dumps({"result": {"apps": rows}}), encoding="utf-8")
            return Mock(returncode=0)
        self.installs += 1
        if self.install_fails:
            return Mock(returncode=1)
        metadata = bundle_metadata(Path(command[command.index("--device") + 2]), platform="ios")
        if self.report_installed_version:
            self.apps[metadata.bundle_id] = (metadata.short_version, metadata.build_version)
        output.write_text("{}", encoding="utf-8")
        return Mock(returncode=0)


class AppleUpgradeTests(unittest.TestCase):
    def test_clean_replacement_does_not_merge_stale_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = app_bundle(root / "installed", "YoloTrade.app", version="1", extra=True)
            candidate = app_bundle(root / "build", "YoloTrade.app", version="2")
            result = replace_macos_app(candidate, old, root / "backups", run=apple_runner)
            self.assertEqual(bundle_metadata(old).short_version, "2")
            self.assertFalse((old / "Contents/Resources/stale.txt").exists())
            self.assertEqual(bundle_metadata(Path(result.backup)).short_version, "1")

    def test_wrong_bundle_id_never_moves_existing_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = app_bundle(root / "installed", "YoloTrade.app", version="1")
            candidate = app_bundle(root / "build", "Other.app", bundle_id="example.other", version="2")
            with self.assertRaises(AppleUpgradeError):
                replace_macos_app(candidate, installed, root / "backups", run=apple_runner)
            self.assertEqual(bundle_metadata(installed).short_version, "1")

    def test_failed_final_switch_restores_old_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = app_bundle(root / "installed", "YoloTrade.app", version="1", extra=True)
            candidate = app_bundle(root / "build", "YoloTrade.app", version="2")
            calls = 0
            def replace(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated switch failure")
                return Path(source).replace(destination)
            with self.assertRaises(AppleUpgradeError):
                replace_macos_app(candidate, installed, root / "backups", run=apple_runner, replace=replace)
            self.assertEqual(bundle_metadata(installed).short_version, "1")
            self.assertTrue((installed / "Contents/Resources/stale.txt").is_file())

    def test_restore_keeps_replaced_candidate_as_another_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = app_bundle(root / "installed", "YoloTrade.app", version="2")
            backup = app_bundle(root / "backups", "YoloTrade-before.app", version="1")
            result = restore_macos_app(backup, installed, root / "backups", run=apple_runner)
            self.assertEqual(bundle_metadata(installed).short_version, "1")
            self.assertEqual(bundle_metadata(Path(result.backup)).short_version, "2")

    def test_ios_upgrade_requires_same_existing_identity_and_verifies_version(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = app_bundle(Path(directory), "YoloTradeIOS.app", version="2", platform="ios")
            runner = DeviceRunner({"com.yoloagents.yolotrade": ("1", "1")})
            result = replace_ios_app(candidate, "fixture-device", run=runner)
        self.assertTrue(result.replaced_existing)
        self.assertEqual(result.metadata.short_version, "2")
        self.assertEqual(runner.installs, 1)

    def test_ios_upgrade_blocks_legacy_identity_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = app_bundle(Path(directory), "YoloTradeIOS.app", version="2", platform="ios")
            runner = DeviceRunner({
                "com.yoloagents.yolotrade": ("1", "1"),
                "com.yoloagents.tradingagent": ("1", "1"),
            })
            with self.assertRaises(AppleUpgradeError):
                replace_ios_app(candidate, "fixture-device", run=runner)
        self.assertEqual(runner.installs, 0)

    def test_ios_upgrade_requires_an_existing_current_app(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = app_bundle(Path(directory), "YoloTradeIOS.app", version="2", platform="ios")
            runner = DeviceRunner({})
            with self.assertRaises(AppleUpgradeError):
                replace_ios_app(candidate, "fixture-device", run=runner)
        self.assertEqual(runner.installs, 0)

    def test_ios_install_failure_is_reported_without_false_success(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = app_bundle(Path(directory), "YoloTradeIOS.app", version="2", platform="ios")
            runner = DeviceRunner({"com.yoloagents.yolotrade": ("1", "1")}, install_fails=True)
            with self.assertRaises(AppleUpgradeError):
                replace_ios_app(candidate, "fixture-device", run=runner)
        self.assertEqual(runner.apps["com.yoloagents.yolotrade"], ("1", "1"))

    def test_ios_version_mismatch_attempts_explicit_rollback_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = app_bundle(root / "new", "YoloTradeIOS.app", version="2", platform="ios")
            rollback = app_bundle(root / "old", "YoloTradeIOS.app", version="1", platform="ios")
            runner = DeviceRunner(
                {"com.yoloagents.yolotrade": ("1", "1")}, report_installed_version=False,
            )
            with self.assertRaises(AppleUpgradeError):
                replace_ios_app(candidate, "fixture-device", rollback_candidate=rollback, run=runner)
        self.assertEqual(runner.installs, 2)


if __name__ == "__main__":
    unittest.main()
