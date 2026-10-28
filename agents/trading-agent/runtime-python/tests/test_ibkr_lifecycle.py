"""Persistent owner-loop lifecycle; fake broker clients and local HTTP only."""

import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from yolo_agents.agent_runtime import create_runtime
from yolo_agents.api import ChatHTTPServer
from yolo_agents.auth import DeviceAuth
from yolo_agents.service import ChatService
from yolo_agents.store import Store
from yolo_trading.__main__ import run_trading_host
from yolo_trading.api_extension import TradingAPIExtension
from yolo_trading.ibkr import IBKRAdapter
from yolo_trading.ibkr_config import IBKRConfig
from yolo_trading.ibkr_lifecycle import IBKRBrokerLifecycle

from test_ibkr import Client, Event


class OwnerLoopClient(Client):
    def __init__(self, *, account="DUexample", failure=None, pending=False,
                 read_failure=None, block_read=None):
        super().__init__()
        self.accounts = [account]
        self.loop = asyncio.get_running_loop()
        self.owner = threading.get_ident()
        self.access_threads = []
        self.failure = failure
        self.read_failure = read_failure
        if pending:
            self.release = asyncio.Event()
        if block_read:
            self.read_release[block_read] = asyncio.Event()
        for name in ("openOrderEvent", "orderStatusEvent", "execDetailsEvent", "commissionReportEvent"):
            setattr(self, name, Event())

    def isConnected(self):
        self.access_threads.append(threading.get_ident())
        return super().isConnected()

    def disconnect(self):
        self.access_threads.append(threading.get_ident())
        super().disconnect()


class LifecycleTests(unittest.TestCase):
    def build(self, *, environment="paper", account="DUexample", failure=None, failures=None,
              pending=False, read_failures=None, block_reads=None, execution=False,
              managed_accounts=None, reconnect_interval=0.05):
        clients = []
        def selected(values, index, default=None):
            if values is None:
                return default
            return values[index] if index < len(values) else values[-1]
        def factory():
            index = len(clients)
            client = OwnerLoopClient(account=account, failure=selected(failures, index, failure), pending=pending,
                                     read_failure=selected(read_failures, index),
                                     block_read=selected(block_reads, index))
            if managed_accounts is not None:
                client.accounts = list(managed_accounts)
            clients.append(client)
            return client
        config = IBKRConfig(environment, account=account, connect_timeout=30,
                            read_only=not execution, execution_enabled=execution)
        broker = IBKRAdapter(config, client_factory=factory)
        lifecycle = IBKRBrokerLifecycle(broker, poll_interval=0.01, reconnect_interval=reconnect_interval)
        self.addCleanup(lifecycle.close)
        return broker, lifecycle, clients

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.005)
        self.fail("background lifecycle did not reach expected state")

    def test_start_owns_a_persistent_loop_and_threadsafe_health(self):
        _, lifecycle, clients = self.build()
        self.assertTrue(lifecycle.start())
        self.wait(lambda: lifecycle.health()["state"] == "connected_read_only")
        client = clients[0]
        self.assertNotEqual(client.owner, threading.get_ident())
        self.assertTrue(client.loop.is_running())
        self.assertFalse(lifecycle.health()["execution_enabled"])
        self.assertTrue(lifecycle.health()["data_complete"])
        self.assertTrue(lifecycle.health()["ready"])
        self.assertEqual(lifecycle.health()["reconciliation_status"]["phase"], "ready")
        # A scheduled event after connect returns must still be processed.
        client.loop.call_soon_threadsafe(client.errorEvent.emit, -1, 1100, "private account detail")
        self.wait(lambda: lifecycle.health()["state"] == "broker_unavailable")
        self.assertNotIn("private", json.dumps(lifecycle.health()))
        self.assertEqual(set(client.access_threads), {client.owner})
        self.assertTrue(lifecycle.close())
        self.assertFalse(lifecycle.health()["lifecycle"]["thread_alive"])
        self.assertTrue(client.loop.is_closed())
        self.assertFalse(client.connected)
        self.assertEqual(client.errorEvent.handlers, [])

    def test_start_and_close_are_idempotent(self):
        _, lifecycle, clients = self.build()
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["connected"])
        self.assertFalse(lifecycle.start())
        self.assertTrue(lifecycle.close())
        self.assertTrue(lifecycle.close())
        self.assertFalse(lifecycle.start())
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(clients[0].calls), 1)
        self.assertEqual(clients[0].disconnects, 1)

    def test_order_recovery_plan_is_idempotent_before_start_and_immutable_after(self):
        _, lifecycle, _ = self.build(account="DU123456", execution=True)
        plan = {"active_order_refs": ["recovery-ref"], "records": []}
        self.assertTrue(lifecycle.stage_order_recovery(plan))
        plan["active_order_refs"].append("mutated-after-stage")
        self.assertTrue(lifecycle.stage_order_recovery({"active_order_refs": ["recovery-ref"], "records": []}))
        self.assertFalse(lifecycle.stage_order_recovery({"active_order_refs": ["other-ref"], "records": []}))
        self.assertTrue(lifecycle.start())
        self.wait(lambda: lifecycle.health()["ready"])
        self.assertFalse(lifecycle.stage_order_recovery({"active_order_refs": ["recovery-ref"], "records": []}))

    def test_order_recovery_exception_blocks_connection_and_stays_failed(self):
        broker, lifecycle, clients = self.build(account="DU123456", execution=True)

        async def fail_recovery(plan):
            raise RuntimeError("private persisted value")

        broker.restore_tracked_orders = fail_recovery
        self.assertTrue(lifecycle.start())
        self.wait(lambda: lifecycle.health()["lifecycle"]["state"] == "failed")
        health = lifecycle.health()
        self.assertEqual(health["lifecycle"]["error"]["code"], "order_recovery_failed")
        self.assertFalse(health["connected"])
        self.assertFalse(health["ready"])
        self.assertEqual(clients, [])
        self.assertNotIn("private", json.dumps(health))

    def test_live_profile_never_starts_a_connection(self):
        _, lifecycle, clients = self.build(environment="live", account="Uexample")
        self.assertFalse(lifecycle.start())
        self.assertFalse(lifecycle.start())
        health = lifecycle.health()
        self.assertEqual(health["state"], "automatic_connection_blocked")
        self.assertEqual(len(health["blockers"]), 1)
        self.assertFalse(health["connection_attempted"])
        self.assertEqual(clients, [])

    def test_foreign_data_is_rejected_before_ledger_or_broker_start(self):
        from types import SimpleNamespace
        from yolo_agents.identity import ensure_agent_identity
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ensure_agent_identity(root, "b-agent")
            args = SimpleNamespace(data_dir=root, adopt_existing_data=False)
            before = sorted(path.name for path in root.iterdir())
            with patch("yolo_trading.__main__.IBKRBrokerLifecycle") as lifecycle:
                with self.assertRaises(RuntimeError):
                    run_trading_host(args, object())
                lifecycle.assert_not_called()
            self.assertEqual(sorted(path.name for path in root.iterdir()), before)
            self.assertFalse((root / "paper-orders.sqlite3").exists())

    def test_missing_account_and_unconfigured_host_do_not_connect(self):
        _, lifecycle, clients = self.build(account=None)
        self.assertFalse(lifecycle.start())
        self.assertEqual(lifecycle.health()["state"], "configuration_incomplete")
        self.assertEqual(clients, [])
        with patch("yolo_trading.__main__.run_host", return_value=0) as run:
            self.assertEqual(run_trading_host(object(), None), 0)
        self.assertEqual(run.call_args.kwargs["extension"].broker_health()["state"], "not_configured")

    def test_gateway_failure_retries_and_recovers_with_full_reconciliation(self):
        _, lifecycle, clients = self.build(failures=[OSError("secret diagnostic"), None])
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["ready"])
        self.assertTrue(lifecycle.health()["lifecycle"]["thread_alive"])
        self.assertTrue(lifecycle.health()["connected"])
        self.assertNotIn("secret", json.dumps(lifecycle.health()))
        self.assertGreaterEqual(len(clients), 2)
        self.assertEqual(clients[1].read_calls,
                         ["positions", "open_orders", "executions", "account_summary"])
        self.assertGreaterEqual(lifecycle.health()["lifecycle"]["recovery_attempts"], 2)
        self.assertTrue(lifecycle.close())

    def test_account_mismatch_requires_manual_fix_without_retry_loop(self):
        _, lifecycle, clients = self.build(managed_accounts=["Uother"], reconnect_interval=0.02)
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["state"] == "account_mismatch")
        attempts = lifecycle.health()["lifecycle"]["recovery_attempts"]
        time.sleep(0.1)
        health = lifecycle.health()
        self.assertEqual(len(clients), 1)
        self.assertEqual(health["lifecycle"]["recovery_attempts"], attempts)
        self.assertFalse(health["lifecycle"]["retry_scheduled"])
        self.assertFalse(health["ready"])
        self.assertNotIn("Uother", json.dumps(health))

    def test_disconnect_revokes_ready_then_new_generation_reconciles_before_execution(self):
        _, lifecycle, clients = self.build(account="DU123456", execution=True,
                                            block_reads=[None, "positions"])
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["execution_enabled"])
        first = lifecycle.health()
        old_client = clients[0]
        old_error_handler = old_client.errorEvent.handlers[0]
        old_client.loop.call_soon_threadsafe(old_client.disconnect)
        self.wait(lambda: len(clients) >= 2 and lifecycle.health()["reconciliation_status"]["phase"] == "reconciling")
        during = lifecycle.health()
        self.assertFalse(during["ready"])
        self.assertFalse(during["data_complete"])
        self.assertFalse(during["execution_enabled"])
        self.assertIsNone(during["session_id"])
        self.assertGreater(during["reconciliation_status"]["connection_generation"],
                           first["reconciliation_status"]["connection_generation"])
        second = clients[1]
        second.loop.call_soon_threadsafe(second.read_release["positions"].set)
        self.wait(lambda: lifecycle.health()["execution_enabled"])
        recovered = lifecycle.health()
        self.assertNotEqual(recovered["session_id"], first["session_id"])
        self.assertEqual(second.read_calls,
                         ["positions", "open_orders", "executions", "account_summary"])
        generation = recovered["reconciliation_status"]["generation"]
        second.loop.call_soon_threadsafe(old_error_handler, -1, 1100, "late private event")
        time.sleep(0.03)
        after_late = lifecycle.health()
        self.assertTrue(after_late["ready"])
        self.assertEqual(after_late["reconciliation_status"]["generation"], generation)
        self.assertNotIn("private", json.dumps(after_late))

    def test_upstream_restore_reconciles_all_reads_on_same_connection_before_ready(self):
        _, lifecycle, clients = self.build(account="DU123456", execution=True)
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["execution_enabled"])
        client = clients[0]
        initial = lifecycle.health()
        self.assertEqual(client.read_calls,
                         ["positions", "open_orders", "executions", "account_summary"])

        client.loop.call_soon_threadsafe(client.errorEvent.emit, -1, 1100, "private outage")
        self.wait(lambda: lifecycle.health()["state"] == "broker_unavailable")
        lost = lifecycle.health()
        self.assertFalse(lost["ready"])
        self.assertFalse(lost["execution_enabled"])
        self.assertIsNone(lost["session_id"])

        positions_release = asyncio.Event()
        client.read_release["positions"] = positions_release
        client.loop.call_soon_threadsafe(client.errorEvent.emit, -1, 1102, "private restored")
        self.wait(lambda: lifecycle.health()["reconciliation_status"]["phase"] == "reconciling")
        during = lifecycle.health()
        self.assertFalse(during["ready"])
        self.assertFalse(during["execution_enabled"])
        self.assertEqual(len(clients), 1)

        client.loop.call_soon_threadsafe(positions_release.set)
        self.wait(lambda: lifecycle.health()["execution_enabled"])
        recovered = lifecycle.health()
        self.assertEqual(client.read_calls,
                         ["positions", "open_orders", "executions", "account_summary"] * 2)
        self.assertGreater(recovered["reconciliation_status"]["generation"],
                           initial["reconciliation_status"]["generation"])
        self.assertNotEqual(recovered["session_id"], initial["session_id"])
        self.assertNotIn("private", json.dumps(recovered))

    def test_failed_reconciliation_retries_all_reads_and_never_publishes_partial_ready(self):
        _, lifecycle, clients = self.build(read_failures=["open_orders", None])
        lifecycle.start()
        self.wait(lambda: clients and clients[0].read_calls)
        self.wait(lambda: lifecycle.health()["state"] == "reconciliation_failed")
        failed = lifecycle.health()
        self.assertFalse(failed["ready"])
        self.assertFalse(failed["data_complete"])
        self.assertEqual(failed["reconciliation_status"]["failure"]["step"], "open_orders")
        self.wait(lambda: lifecycle.health()["ready"])
        self.assertGreaterEqual(len(clients), 2)
        self.assertEqual(clients[1].read_calls,
                         ["positions", "open_orders", "executions", "account_summary"])
        self.assertNotIn("sensitive", json.dumps(lifecycle.health()))

    def test_close_cancels_pending_connect_without_waiting_for_connect_timeout(self):
        _, lifecycle, clients = self.build(pending=True)
        lifecycle.start()
        self.wait(lambda: clients and clients[0].calls)
        start = time.monotonic()
        self.assertTrue(lifecycle.close(timeout=2))
        self.assertLess(time.monotonic() - start, 2)
        self.assertTrue(clients[0].loop.is_closed())
        self.assertFalse(lifecycle.health()["connected"])
        self.assertEqual(clients[0].errorEvent.handlers, [])

    def test_close_during_thread_startup_still_cleans_up(self):
        _, lifecycle, clients = self.build()
        lifecycle.start()
        self.assertTrue(lifecycle.close())
        self.assertFalse(lifecycle.health()["lifecycle"]["thread_alive"])
        self.assertTrue(all(not client.connected and client.loop.is_closed() for client in clients))

    def test_health_returns_independent_snapshots(self):
        _, lifecycle, _ = self.build()
        health = lifecycle.health()
        health["configuration"]["execution_enabled"] = True
        health["blockers"].append({"code": "mutated"})
        self.assertFalse(lifecycle.health()["configuration"]["execution_enabled"])
        self.assertEqual(lifecycle.health()["blockers"], [])

    def test_thread_start_failure_is_sanitized_and_nonfatal(self):
        _, lifecycle, clients = self.build()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("private diagnostic")):
            self.assertFalse(lifecycle.start())
        health = lifecycle.health()
        self.assertEqual(health["lifecycle"]["state"], "failed")
        self.assertTrue(health["health_stale"])
        self.assertNotIn("private", json.dumps(health))
        self.assertEqual(clients, [])

    def test_disconnect_failure_is_reported_without_hanging_shutdown(self):
        _, lifecycle, clients = self.build(account="DU123456", execution=True)
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["execution_enabled"])
        before = lifecycle.health()
        clients[0].disconnect_failure = True
        self.assertFalse(lifecycle.close(timeout=2))
        health = lifecycle.health()
        self.assertFalse(health["lifecycle"]["thread_alive"])
        self.assertTrue(clients[0].loop.is_closed())
        self.assertEqual(health["lifecycle"]["error"]["code"], "broker_disconnect_failed")
        self.assertTrue(health["health_stale"])
        self.assertFalse(health["ready"])
        self.assertFalse(health["data_complete"])
        self.assertFalse(health["execution_enabled"])
        self.assertIsNone(health["session_id"])
        self.assertGreater(health["reconciliation_status"]["generation"],
                           before["reconciliation_status"]["generation"])
        self.assertNotIn("sensitive", json.dumps(health))

    def test_missing_sdk_is_visible_without_crashing_host(self):
        broker = IBKRAdapter(IBKRConfig("paper", account="DUexample"))
        with patch("yolo_trading.ibkr.importlib.util.find_spec", return_value=None):
            lifecycle = IBKRBrokerLifecycle(broker, poll_interval=0.01)
            self.addCleanup(lifecycle.close)
            lifecycle.start()
            self.wait(lambda: lifecycle.health()["lifecycle"]["state"] == "running")
            self.assertEqual(lifecycle.health()["state"], "dependency_missing")
            self.assertFalse(lifecycle.health()["connection_attempted"])
            self.assertTrue(lifecycle.close())

    def test_host_wrapper_closes_broker_even_when_host_initialization_fails(self):
        for failure in (None, RuntimeError("host binding failed"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                broker, _, clients = self.build()
                extension_seen = []
                def host(args, *, extension, agent_name, app_name):
                    extension_seen.append(extension)
                    self.wait(lambda: extension.broker_health()["connected"])
                    self.assertEqual(agent_name, "trading-agent")
                    self.assertEqual(app_name, "Trading Agent")
                    if failure is not None:
                        raise failure
                    return 7
                with patch("yolo_trading.__main__.run_host", side_effect=host):
                    if failure is None:
                        self.assertEqual(run_trading_host(object(), broker), 7)
                    else:
                        with self.assertRaises(type(failure)):
                            run_trading_host(object(), broker)
                self.assertTrue(clients[0].loop.is_closed())
                self.assertFalse(clients[0].connected)
                self.assertEqual(extension_seen[0].broker_health()["lifecycle"]["state"], "stopped")

    def test_http_health_tracks_connection_and_chat_works_when_broker_fails(self):
        _, lifecycle, clients = self.build()
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["connected"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / "app.sqlite3")
            runtime = create_runtime("demo", None, root, store)
            auth = DeviceAuth(root / "devices.sqlite3")
            app = ChatService(store, runtime)
            server = ChatHTTPServer(("127.0.0.1", 0), app, auth, extension=TradingAPIExtension(lifecycle))
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            def request(method, path, data=None, token=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = "Bearer " + token
                try:
                    connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
            try:
                status, body = request("GET", "/v1/health")
                self.assertEqual(status, 200)
                self.assertEqual(body["broker"]["state"], "connected_read_only")
                self.assertFalse(body["trade_execution_enabled"])
                self.assertNotIn("DUexample", json.dumps(body))
                _, pair = request("POST", "/v1/devices/pair", {
                    "pairing_code": auth.issue_pairing_code("macos"), "device_name": "test", "platform": "macos"})
                token = pair["token"]
                clients[0].loop.call_soon_threadsafe(clients[0].disconnect)
                self.wait(lambda: lifecycle.health()["state"] == "gateway_unavailable")
                status, body = request("GET", "/v1/broker/health", token=token)
                self.assertEqual(status, 200)
                self.assertFalse(body["broker"]["connected"])
                self.assertEqual(body["broker"]["state"], "gateway_unavailable")
                self.assertEqual(request("POST", "/v1/conversations", {"title": "broker offline"}, token)[0], 201)
            finally:
                app.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
                runtime.close()
                store.close()
                lifecycle.close()
                self.assertFalse(worker.is_alive(), "HTTP test server thread did not exit")


if __name__ == "__main__":
    unittest.main()
