"""IBKR config is explicit, read-only and independent of optional SDK imports."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yolo_trading.ibkr import IBKRAdapter, main
from yolo_trading.ibkr_config import ENV_PREFIX, IBKRConfig, IBKRConfigurationError, load_ibkr_config


class ConfigurationTests(unittest.TestCase):
    def load(self, base=None, **kwargs):
        return load_ibkr_config(base, environ=kwargs.pop("environ", {}), **kwargs)

    def test_environment_is_required_and_not_inferred_from_port(self):
        for base in (None, {}, {"port": 4002}, {"environment": "demo"}, {"environment": ""}):
            with self.subTest(base=base), self.assertRaises(IBKRConfigurationError):
                self.load(base)

    def test_environment_defaults_are_read_only(self):
        for environment, port in (("paper", 4002), ("live", 4001)):
            config = self.load({"environment": environment})
            self.assertEqual(config.port, port)
            self.assertIs(config.read_only, True)
            self.assertIs(config.execution_enabled, False)
            self.assertFalse(config.audit()["execution_supported"])

    def test_custom_ports_never_change_environment(self):
        for environment, port in (("paper", 4001), ("live", 4002), ("live", 43123)):
            config = self.load({"environment": environment, "port": port})
            self.assertEqual((config.environment, config.port), (environment, port))

    def test_overrides_and_sources_are_auditable(self):
        config = self.load({"environment": "paper", "client_id": 21, "account": "DUexample"},
                           environ={ENV_PREFIX + "PORT": "43123", ENV_PREFIX + "CLIENT_ID": "22"},
                           overrides={"client_id": 23, "host": "127.0.0.2"})
        audit = config.audit()
        self.assertEqual((config.port, config.client_id, config.host), (43123, 23, "127.0.0.2"))
        self.assertEqual(audit["sources"]["port"], "environment")
        self.assertEqual(audit["sources"]["client_id"], "override")
        self.assertEqual(audit["sources"]["environment"], "base")
        self.assertNotIn("DUexample", json.dumps(audit))
        self.assertNotIn("DUexample", repr(config))
        other = self.load({"environment": "paper", "account": "DUother"})
        self.assertNotEqual(other.config_id, config.config_id)

    def test_environment_switch_discards_previous_profile(self):
        base = {"environment": "paper", "host": "127.0.0.2", "port": 43123,
                "client_id": 31, "account": "DUexample", "connect_timeout": 9}
        config = self.load(base, overrides={"environment": "live"})
        self.assertEqual((config.host, config.port, config.client_id), ("127.0.0.1", 4001, 20))
        self.assertIsNone(config.account)
        self.assertEqual(config.connect_timeout, 5)
        self.assertTrue(config.read_only)
        self.assertFalse(config.execution_enabled)
        self.assertEqual(config.audit()["sources"]["port"], "environment_default")

    def test_environment_switch_keeps_only_explicit_new_values(self):
        config = self.load({"environment": "paper", "account": "DUexample"},
                           environ={ENV_PREFIX + "ENVIRONMENT": "live", ENV_PREFIX + "PORT": "49123"},
                           overrides={"account": "Uexample", "client_id": 27})
        self.assertEqual((config.environment, config.port, config.account, config.client_id),
                         ("live", 49123, "Uexample", 27))

    def test_same_environment_overlay_keeps_profile(self):
        config = self.load({"environment": "paper", "account": "DUexample", "port": 49123},
                           overrides={"environment": "paper", "client_id": 27})
        self.assertEqual((config.account, config.port, config.client_id), ("DUexample", 49123, 27))

    def test_both_environments_reject_execution_and_writable_mode(self):
        for environment in ("paper", "live"):
            for field, value, code in (("execution_enabled", True, "execution_not_supported"),
                                       ("execution_enabled", "true", "execution_not_supported"),
                                       ("read_only", False, "read_only_required")):
                with self.subTest(environment=environment, field=field):
                    with self.assertRaises(IBKRConfigurationError) as failure:
                        self.load({"environment": environment}, overrides={field: value})
                    self.assertEqual(failure.exception.code, code)

    def test_config_rejects_ambiguous_numbers_and_booleans(self):
        cases = {"port": [0, 65536, True, "4e3", 4002.0, "9" * 5000],
                 "client_id": [0, -1, True, "1.0", 2147483648],
                 "read_only": ["yes", 1, None], "execution_enabled": ["0", 0, None],
                 "connect_timeout": [0, -1, True, float("nan"), float("inf"), 61]}
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=str(value)[:20]), self.assertRaises(IBKRConfigurationError):
                    self.load({"environment": "paper", field: value})

    def test_loopback_addresses_only(self):
        for host, normalized in (("localhost", "127.0.0.1"), ("127.0.0.2", "127.0.0.2"), ("::1", "::1")):
            self.assertEqual(IBKRConfig("paper", host=host).host, normalized)
        for host in ("0.0.0.0", "192.168.1.5", "example.com", "localhost.example", "::", "", None):
            with self.subTest(host=host), self.assertRaises(IBKRConfigurationError):
                IBKRConfig("paper", host=host)

    def test_account_is_explicit_and_not_derived_from_prefix(self):
        self.assertEqual(IBKRConfig("paper", account="Uexample").environment, "paper")
        self.assertEqual(IBKRConfig("live", account="DUexample").environment, "live")
        for account in ("", "with spaces", "user:password", True, "中文"):
            with self.subTest(account=account), self.assertRaises(IBKRConfigurationError):
                IBKRConfig("paper", account=account)

    def test_unknown_fields_and_credentials_are_not_echoed(self):
        for base in ({"environment": "paper", "password": "never-print-me"}, [], "bad"):
            with self.assertRaises(IBKRConfigurationError) as failure:
                self.load(base)
            self.assertNotIn("never-print-me", str(failure.exception))

    def test_frozen_configuration_and_no_order_operations(self):
        config = IBKRConfig("live")
        with self.assertRaises(AttributeError):
            config.execution_enabled = True
        adapter = IBKRAdapter(config)
        for name in ("placeOrder", "place_order", "cancelOrder", "cancel_order", "submit_order"):
            self.assertFalse(hasattr(adapter, name))


class InspectionTests(unittest.TestCase):
    def inspect(self, arguments):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            status = main(arguments)
        return status, json.loads(output.getvalue())

    def test_cli_is_offline_and_reports_missing_sdk_and_account(self):
        with patch("yolo_trading.ibkr.importlib.util.find_spec", return_value=None), \
             patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
             patch("yolo_trading.ibkr.importlib.import_module", side_effect=AssertionError("SDK import forbidden")):
            status, body = self.inspect(["--environment", "live"])
        self.assertEqual(status, 1)
        self.assertEqual(body["state"], "dependency_missing")
        self.assertEqual({item["code"] for item in body["blockers"]}, {"dependency_missing", "account_required"})
        self.assertFalse(body["connection_attempted"])
        self.assertFalse(body["execution_enabled"])
        self.assertFalse(body["environment_verified"])

    def test_cli_reports_configuration_errors_without_connection(self):
        for args in ([], ["--environment", "live", "--execution-enabled", "true"],
                     ["--environment", "paper", "--read-only", "false"]):
            with self.subTest(args=args):
                status, body = self.inspect(args)
                self.assertEqual(status, 2)
                self.assertEqual(body["state"], "configuration_error")

    def test_cli_reads_json_then_applies_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"environment": "paper", "port": 49123, "client_id": 26}))
            with patch("yolo_trading.ibkr.importlib.util.find_spec", return_value=None):
                _, body = self.inspect(["--config", str(path), "--port", "49124"])
            self.assertEqual(body["configuration"]["port"], 49124)
            self.assertEqual(body["configuration"]["client_id"], 26)
            path.write_text("{not valid JSON")
            status, body = self.inspect(["--config", str(path)])
            self.assertEqual(status, 2)
            self.assertEqual(body["error"]["code"], "config_file_unreadable")


if __name__ == "__main__":
    unittest.main()
