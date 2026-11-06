from pathlib import Path
import plistlib
import shutil
import tempfile
import unittest
from unittest.mock import Mock

from yolo_trade.apple_upgrade import (
    AppleUpgradeError, bundle_metadata, replace_macos_app, restore_macos_app,
)


def app_bundle(root: Path, name: str, *, bundle_id: str = "com.yoloagents.yolotrade",
               version: str = "1", extra: bool = False) -> Path:
    app = root / name
    executable = app / "Contents/MacOS/YoloTradeMac"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    executable.chmod(0o755)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": "YoloTradeMac",
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


if __name__ == "__main__":
    unittest.main()
