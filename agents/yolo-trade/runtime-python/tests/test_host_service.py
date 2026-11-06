import json
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from yolo_trade.host_service import (
    HostServiceError, _paths, install, launch_agent_payload, status, validate_paper_config,
)


class HostServiceTests(unittest.TestCase):
    def test_config_requires_explicit_paper_account(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ibkr.json"
            path.write_text(json.dumps({"environment": "paper"}), encoding="utf-8")
            with self.assertRaises(HostServiceError):
                validate_paper_config(path)
            path.write_text(json.dumps({"environment": "live", "account": "U123456"}), encoding="utf-8")
            with self.assertRaises(HostServiceError):
                validate_paper_config(path)
            path.write_text(json.dumps({
                "environment": "paper", "account": "DU123456", "read_only": True,
                "execution_enabled": False,
            }), encoding="utf-8")
            with self.assertRaises(HostServiceError):
                validate_paper_config(path)

    def test_payload_is_loopback_persistent_and_uses_product_paths(self):
        paths = _paths(Path("/tmp/yolo-host-service"))
        payload = launch_agent_payload(paths, Path("/repo/bin/yolo-trade-host"))
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        command = payload["ProgramArguments"]
        self.assertIn("127.0.0.1", command)
        self.assertIn("8765", command)
        self.assertIn(str(paths["config"]), command)
        self.assertNotIn("account", " ".join(command).lower())

    def test_install_copies_validated_private_config_and_loads_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source.json"
            source.write_text(json.dumps({
                "environment": "paper", "account": "DU123456", "read_only": False,
                "execution_enabled": True, "client_id": 20,
            }), encoding="utf-8")
            loaded = Mock(returncode=0)
            with patch("yolo_trade.host_service.subprocess.run", return_value=loaded):
                plist = install(source, home=home)
            paths = _paths(home)
            self.assertEqual(plist, paths["plist"])
            self.assertEqual(stat.S_IMODE(paths["config"].stat().st_mode), 0o600)
            stored = json.loads(paths["config"].read_text(encoding="utf-8"))
            self.assertEqual(stored["environment"], "paper")
            self.assertEqual(stored["account"], "DU123456")
            self.assertFalse(stored["read_only"])
            self.assertTrue(stored["execution_enabled"])
            self.assertTrue(paths["plist"].is_file())

    def test_failed_upgrade_restores_old_config_plist_and_loaded_service(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = _paths(home)
            paths["config"].parent.mkdir(parents=True)
            old_config = b'{"old": true}\n'
            old_plist = plistlib.dumps({"Label": "old"})
            paths["config"].write_bytes(old_config)
            paths["config"].chmod(0o600)
            paths["plist"].parent.mkdir(parents=True)
            paths["plist"].write_bytes(old_plist)
            source = home / "source.json"
            source.write_text(json.dumps({
                "environment": "paper", "account": "DU123456", "read_only": False,
                "execution_enabled": True,
            }), encoding="utf-8")
            bootstraps = 0
            def run(command, **_kwargs):
                nonlocal bootstraps
                if "bootstrap" in command:
                    bootstraps += 1
                    return Mock(returncode=1 if bootstraps == 1 else 0)
                return Mock(returncode=0)
            with patch("yolo_trade.host_service.subprocess.run", side_effect=run):
                with self.assertRaises(HostServiceError):
                    install(source, home=home)
            self.assertEqual(paths["config"].read_bytes(), old_config)
            self.assertEqual(paths["plist"].read_bytes(), old_plist)
            self.assertEqual(bootstraps, 2)

    def test_failed_first_install_leaves_no_partial_config_or_plist(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "source.json"
            source.write_text(json.dumps({
                "environment": "paper", "account": "DU123456", "read_only": False,
                "execution_enabled": True,
            }), encoding="utf-8")
            def run(_command, **_kwargs):
                return Mock(returncode=1)
            with patch("yolo_trade.host_service.subprocess.run", side_effect=run):
                with self.assertRaises(HostServiceError):
                    install(source, home=home)
            paths = _paths(home)
            self.assertFalse(paths["config"].exists())
            self.assertFalse(paths["plist"].exists())

    def test_status_requires_one_listener_correct_identity_and_execution_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = _paths(home)
            paths["config"].parent.mkdir(parents=True)
            paths["config"].write_text(json.dumps({
                "environment": "paper", "account": "DU123456", "read_only": False,
                "execution_enabled": True,
            }), encoding="utf-8")
            def run(command, **_kwargs):
                if command[0] == "/usr/sbin/lsof":
                    return Mock(returncode=0, stdout="321\n321\n")
                return Mock(returncode=0)
            health = {
                "status": "ok", "agent_id": "yolo-trade", "broker_state": "connected_paper",
                "broker_ready": True, "data_complete": True,
            }
            result = status(home=home, runner=run, health_reader=lambda: health)
        self.assertTrue(result["service_ready"])
        self.assertEqual(result["listener_count"], 1)
        self.assertNotIn("account", json.dumps(result))

    def test_status_fails_closed_with_duplicate_listeners(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = _paths(home)
            paths["config"].parent.mkdir(parents=True)
            paths["config"].write_text(json.dumps({
                "environment": "paper", "account": "DU123456", "read_only": False,
                "execution_enabled": True,
            }), encoding="utf-8")
            def run(command, **_kwargs):
                if command[0] == "/usr/sbin/lsof":
                    return Mock(returncode=0, stdout="321\n654\n")
                return Mock(returncode=0)
            result = status(
                home=home, runner=run,
                health_reader=lambda: {"status": "ok", "agent_id": "yolo-trade"},
            )
        self.assertFalse(result["single_listener"])
        self.assertFalse(result["service_ready"])


if __name__ == "__main__":
    unittest.main()
