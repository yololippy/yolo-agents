import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from yolo_trade.gateway_supervisor import (
    KEYCHAIN_PASSWORD_SERVICE,
    KEYCHAIN_USERNAME_SERVICE,
    GatewaySupervisorError,
    KeychainCredentials,
    SupervisorPaths,
    _read_recovery_marker,
    _redact_ibc_output,
    _write_recovery_marker,
    classify_status,
    gateway_process_ids,
    ibc_command,
    in_ibkr_maintenance_recovery_window,
    port_is_open,
    render_ibc_config,
    terminate_single_gateway,
)


class GatewaySupervisorTests(unittest.TestCase):
    def test_classification_never_treats_running_login_window_as_stopped(self):
        status = classify_status(
            is_port_open=False,
            is_gateway_running=True,
            credentials_ready=True,
            ibc_installed=True,
            is_launch_agent_loaded=True,
        )
        self.assertEqual(status.state, "authentication_required")
        self.assertTrue(status.paper_only)

    def test_ready_port_wins_without_exposing_credentials(self):
        status = classify_status(
            is_port_open=True,
            is_gateway_running=False,
            credentials_ready=False,
            ibc_installed=False,
            is_launch_agent_loaded=False,
        )
        self.assertEqual(status.state, "local_api_ready")
        self.assertTrue(status.port_open)
        self.assertFalse(status.broker_connected)
        self.assertFalse(status.data_ready)
        self.assertNotIn("password", json.dumps(status.__dict__))

    def test_local_upstream_and_data_health_are_distinct(self):
        status = classify_status(
            is_port_open=True, is_gateway_running=True, credentials_ready=True,
            ibc_installed=True, is_launch_agent_loaded=True, gateway_process_count=1,
            host_health={"broker_connected": False, "data_ready": False},
        )
        self.assertEqual(status.state, "local_api_ready")
        self.assertTrue(status.port_open)
        self.assertTrue(status.host_reachable)
        self.assertFalse(status.broker_connected)
        self.assertFalse(status.data_ready)

    def test_multiple_gateway_processes_fail_closed(self):
        status = classify_status(
            is_port_open=True, is_gateway_running=True, credentials_ready=True,
            ibc_installed=True, is_launch_agent_loaded=True, gateway_process_count=2,
        )
        self.assertEqual(status.state, "gateway_conflict")
        self.assertFalse(status.single_instance)

    def test_missing_credentials_and_install_have_distinct_states(self):
        missing = classify_status(
            is_port_open=False,
            is_gateway_running=False,
            credentials_ready=False,
            ibc_installed=True,
            is_launch_agent_loaded=False,
        )
        self.assertEqual(missing.state, "credentials_missing")
        missing_ibc = classify_status(
            is_port_open=False,
            is_gateway_running=False,
            credentials_ready=True,
            ibc_installed=False,
            is_launch_agent_loaded=False,
        )
        self.assertEqual(missing_ibc.state, "ibc_missing")

    def test_ibc_command_is_paper_only_and_contains_no_credentials(self):
        paths = SupervisorPaths.defaults(Path("/tmp/yolo-supervisor-test"))
        command = ibc_command(paths)
        self.assertIn("--mode=paper", command)
        self.assertIn("--user=", command)
        self.assertIn("--pw=", command)
        combined = " ".join(command)
        self.assertNotIn("secret-user", combined)
        self.assertNotIn("secret-password", combined)
        self.assertNotIn("live", combined)

    def test_render_config_is_private_and_preserves_safe_boundaries(self):
        template = """IbLoginId=demo
IbPassword=demo
ReloginAfterSecondFactorAuthenticationTimeout=yes
ExitAfterSecondFactorAuthenticationTimeout=yes
TradingMode=live
AcceptNonBrokerageAccountWarning=no
ExistingSessionDetectedAction=primary
ReadOnlyLogin=yes
ReadOnlyApi=yes
AutoLogoffTime=10:00 PM
AutoRestartTime=
ColdRestartTime=
ClosedownAt=Friday 22:00
AcceptIncomingConnectionAction=accept
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "config.ini", root / "runtime" / "config.ini"
            source.write_text(template, encoding="utf-8")
            render_ibc_config(source, destination, "secret-user", "secret-password")
            output = destination.read_text(encoding="utf-8")
            self.assertIn("TradingMode=paper", output)
            self.assertIn("ReadOnlyLogin=no", output)
            self.assertIn("ReadOnlyApi=\n", output)
            self.assertIn("AutoRestartTime=07:30 AM", output)
            self.assertIn("ColdRestartTime=14:15", output)
            self.assertIn("AcceptIncomingConnectionAction=reject", output)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)

    def test_render_rejects_newline_secret_and_incompatible_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.ini"
            source.write_text("IbLoginId=demo\n", encoding="utf-8")
            with self.assertRaises(GatewaySupervisorError):
                render_ibc_config(source, root / "out.ini", "bad\nuser", "password")
            with self.assertRaises(GatewaySupervisorError):
                render_ibc_config(source, root / "out.ini", "user", "password")

    def test_ibc_log_redaction_removes_fields_and_secret_values(self):
        source = BytesIO(
            b"startup user paper-user\n"
            b"    IbLoginId=paper-user\n"
            b"    IbPassword=paper-password\n"
            b"detected dialog entitled: DUR123456 Trader Workstation Configuration\n"
            b"normal diagnostic\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "logs" / "ibc.log"
            _redact_ibc_output(source, destination, ("paper-user", "paper-password"))
            output = destination.read_text(encoding="utf-8")
            self.assertNotIn("paper-user", output)
            self.assertNotIn("paper-password", output)
            self.assertNotIn("DUR123456", output)
            self.assertIn("<redacted-account>", output)
            self.assertIn("startup user <redacted>", output)
            self.assertIn("IbLoginId=<redacted>", output)
            self.assertIn("IbPassword=<redacted>", output)
            self.assertIn("normal diagnostic", output)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_keychain_reads_only_requested_items_and_never_uses_shell(self):
        values = {
            KEYCHAIN_USERNAME_SERVICE: "paper-user",
            KEYCHAIN_PASSWORD_SERVICE: "paper-password",
        }
        backend = Mock()
        backend.exists.side_effect = lambda service, account: service in values and account == "paper"
        backend.read.side_effect = lambda service, account: values[service]
        credentials = KeychainCredentials(backend)
        self.assertTrue(credentials.ready())
        self.assertEqual(credentials.pair(), ("paper-user", "paper-password"))

    def test_interactive_keychain_write_requires_tty(self):
        credentials = KeychainCredentials(Mock())
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(GatewaySupervisorError):
                credentials.set_interactive()

    def test_interactive_keychain_write_has_clear_prompts_and_no_secrets_in_argv(self):
        backend = Mock()
        credentials = KeychainCredentials(backend)
        with patch("sys.stdin.isatty", return_value=True), patch(
            "sys.stdout.isatty", return_value=True
        ), patch(
            "yolo_trade.gateway_supervisor.getpass.getpass",
            side_effect=("paper-user", "paper-password", "paper-password"),
        ) as prompt:
            credentials.set_interactive()
        self.assertEqual(prompt.call_count, 3)
        self.assertEqual(
            backend.write.call_args_list,
            [
                call(KEYCHAIN_USERNAME_SERVICE, "paper", "paper-user"),
                call(KEYCHAIN_PASSWORD_SERVICE, "paper", "paper-password"),
            ],
        )

    def test_password_mismatch_rolls_back_without_writing(self):
        backend = Mock()
        credentials = KeychainCredentials(backend)
        with patch("sys.stdin.isatty", return_value=True), patch(
            "sys.stdout.isatty", return_value=True
        ), patch(
            "yolo_trade.gateway_supervisor.getpass.getpass",
            side_effect=("paper-user", "first", "second"),
        ):
            with self.assertRaises(GatewaySupervisorError):
                credentials.set_interactive()
        backend.write.assert_not_called()

    def test_keychain_delete_targets_only_the_two_paper_items(self):
        backend = Mock()
        KeychainCredentials(backend).delete()
        self.assertEqual(
            backend.delete.call_args_list,
            [
                call(KEYCHAIN_USERNAME_SERVICE, "paper"),
                call(KEYCHAIN_PASSWORD_SERVICE, "paper"),
            ],
        )

    def test_listener_check_is_passive_and_process_ids_are_counted(self):
        listener = Mock(return_value=Mock(returncode=0))
        self.assertTrue(port_is_open(runner=listener))
        self.assertEqual(listener.call_args.args[0][:2], ["/usr/sbin/lsof", "-nP"])
        process = Mock(return_value=Mock(returncode=0, stdout="123\n456\n123\ninvalid\n"))
        self.assertEqual(gateway_process_ids(process), (123, 456))

    def test_maintenance_windows_include_recovery_grace(self):
        self.assertTrue(in_ibkr_maintenance_recovery_window(datetime(2026, 11, 5, 6, 30)))
        self.assertTrue(in_ibkr_maintenance_recovery_window(datetime(2026, 11, 5, 21, 29)))
        self.assertFalse(in_ibkr_maintenance_recovery_window(datetime(2026, 11, 5, 7, 30)))
        self.assertFalse(in_ibkr_maintenance_recovery_window(datetime(2026, 11, 5, 21, 30)))

    def test_gateway_restart_requires_exactly_one_instance(self):
        single = Mock(return_value=Mock(returncode=0, stdout="123\n"))
        with patch("yolo_trade.gateway_supervisor.os.kill") as stop:
            self.assertTrue(terminate_single_gateway(single))
            stop.assert_called_once_with(123, 15)
        multiple = Mock(return_value=Mock(returncode=0, stdout="123\n456\n"))
        with patch("yolo_trade.gateway_supervisor.os.kill") as stop:
            self.assertFalse(terminate_single_gateway(multiple))
            stop.assert_not_called()

    def test_recovery_marker_persists_the_single_restart_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = SupervisorPaths.defaults(Path(directory))
            _write_recovery_marker(paths, "2110:2026-11-05T21:15:00+00:00", launch_used=False)
            marker = _read_recovery_marker(paths)
            self.assertTrue(marker["restart_requested"])
            self.assertFalse(marker["launch_used"])
            _write_recovery_marker(paths, marker["event_key"], launch_used=True)
            self.assertTrue(_read_recovery_marker(paths)["launch_used"])


if __name__ == "__main__":
    unittest.main()
