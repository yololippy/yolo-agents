import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from yolo_beni.__main__ import build_parser, main


class YoloBeniEntryTests(unittest.TestCase):
    def test_default_identity_and_data_are_yolo_beni_only(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.port, 8766)
        self.assertEqual(args.mode, "chatgpt")
        self.assertEqual(args.data_dir, Path.home() / "Library/Application Support/YoloAgents/yolo-beni")

    def test_host_gets_identity_without_a_trading_extension(self):
        with patch.object(sys, "argv", ["yolo-beni", "--mode", "demo"]), patch(
            "yolo_beni.__main__.run_host", return_value=0
        ) as host:
            self.assertEqual(main(), 0)
        self.assertEqual(host.call_args.kwargs, {"agent_name": "yolo-beni", "app_name": "YoloBeni"})
        self.assertEqual(host.call_args.args[0].mode, "demo")

    def test_trading_arguments_are_not_accepted(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--ibkr-environment", "paper"])

    def test_explicit_isolated_directory_and_port_are_preserved(self):
        args = build_parser().parse_args(["--mode", "demo", "--data-dir", "/tmp/b-fixture", "--port", "0"])
        self.assertEqual(args.data_dir, Path("/tmp/b-fixture"))
        self.assertEqual(args.port, 0)


if __name__ == "__main__":
    unittest.main()
