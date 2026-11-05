import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yolo_beni.__main__ import build_parser, main
from yolo_beni.product import (
    AGENT_ID, DEFAULT_PORT, DISPLAY_NAME, RUNTIME_CAPABILITIES,
)


class YoloBeniEntryTests(unittest.TestCase):
    def test_default_identity_and_data_are_yolo_beni_only(self):
        args = build_parser().parse_args([])
        self.assertEqual(AGENT_ID, "yolo-beni")
        self.assertEqual(DISPLAY_NAME, "YoloBeni")
        self.assertEqual(DEFAULT_PORT, 8766)
        self.assertEqual(args.port, DEFAULT_PORT)
        self.assertEqual(args.mode, "chatgpt")
        self.assertEqual(args.data_dir, Path.home() / "Library/Application Support/YoloAgents/yolo-beni")
        self.assertEqual(RUNTIME_CAPABILITIES.tool_schema_version, 0)
        self.assertIsNone(RUNTIME_CAPABILITIES.pydantic_tools_factory)
        self.assertIsNone(RUNTIME_CAPABILITIES.codex_dynamic_tools_factory)

    def test_host_gets_identity_without_a_trading_extension(self):
        with patch.object(sys, "argv", ["yolo-beni", "--mode", "demo"]), patch(
            "yolo_beni.__main__.run_host", return_value=0
        ) as host:
            self.assertEqual(main(), 0)
        self.assertEqual(host.call_args.kwargs, {
            "agent_name": "yolo-beni", "app_name": "YoloBeni",
            "runtime_capabilities": RUNTIME_CAPABILITIES,
        })
        self.assertEqual(host.call_args.args[0].mode, "demo")

    def test_trading_arguments_are_not_accepted(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--ibkr-environment", "paper"])

    def test_unmarked_trade_database_cannot_be_adopted_by_cli_or_host_args(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--adopt-existing-data"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / "app.sqlite3"
            legacy.write_bytes(b"legacy-trade-database")
            args = SimpleNamespace(data_dir=root, adopt_existing_data=True)
            from yolo_agents.host import run_host
            with self.assertRaises(RuntimeError):
                run_host(args, agent_name=AGENT_ID, app_name=DISPLAY_NAME,
                         runtime_capabilities=RUNTIME_CAPABILITIES)
            self.assertEqual(legacy.read_bytes(), b"legacy-trade-database")
            self.assertFalse((root / ".agent-owner.json").exists())

    def test_explicit_isolated_directory_and_port_are_preserved(self):
        args = build_parser().parse_args(["--mode", "demo", "--data-dir", "/tmp/b-fixture", "--port", "0"])
        self.assertEqual(args.data_dir, Path("/tmp/b-fixture"))
        self.assertEqual(args.port, 0)


if __name__ == "__main__":
    unittest.main()
