import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from yolo_trade.host_service import HostServiceError, _paths, install, launch_agent_payload, validate_paper_config


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
                "environment": "paper", "account": "DU123456", "read_only": True,
                "execution_enabled": False, "client_id": 20,
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
            self.assertTrue(paths["plist"].is_file())


if __name__ == "__main__":
    unittest.main()
