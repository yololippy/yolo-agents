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

from test_ibkr import Client


class OwnerLoopClient(Client):
    def __init__(self, *, failure=None, pending=False):
        super().__init__()
        self.loop = asyncio.get_running_loop()
        self.owner = threading.get_ident()
        self.access_threads = []
        self.failure = failure
        if pending:
            self.release = asyncio.Event()

    def isConnected(self):
        self.access_threads.append(threading.get_ident())
        return super().isConnected()

    def disconnect(self):
        self.access_threads.append(threading.get_ident())
        super().disconnect()


class LifecycleTests(unittest.TestCase):
    def build(self, *, environment="paper", account="DUexample", failure=None, pending=False):
        clients = []
        def factory():
            client = OwnerLoopClient(failure=failure, pending=pending)
            clients.append(client)
            return client
        broker = IBKRAdapter(IBKRConfig(environment, account=account, connect_timeout=30), client_factory=factory)
        lifecycle = IBKRBrokerLifecycle(broker, poll_interval=0.01)
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
        self.assertFalse(lifecycle.health()["data_complete"])
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

    def test_live_profile_never_starts_a_connection(self):
        _, lifecycle, clients = self.build(environment="live", account="Uexample")
        self.assertFalse(lifecycle.start())
        self.assertFalse(lifecycle.start())
        health = lifecycle.health()
        self.assertEqual(health["state"], "automatic_connection_blocked")
        self.assertEqual(len(health["blockers"]), 1)
        self.assertFalse(health["connection_attempted"])
        self.assertEqual(clients, [])

    def test_missing_account_and_unconfigured_host_do_not_connect(self):
        _, lifecycle, clients = self.build(account=None)
        self.assertFalse(lifecycle.start())
        self.assertEqual(lifecycle.health()["state"], "configuration_incomplete")
        self.assertEqual(clients, [])
        with patch("yolo_trading.__main__.run_host", return_value=0) as run:
            self.assertEqual(run_trading_host(object(), None), 0)
        self.assertEqual(run.call_args.kwargs["extension"].broker_health()["state"], "not_configured")

    def test_gateway_failure_does_not_stop_loop_or_retry(self):
        _, lifecycle, clients = self.build(failure=OSError("secret diagnostic"))
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["state"] == "gateway_unavailable")
        self.assertTrue(lifecycle.health()["lifecycle"]["thread_alive"])
        self.assertFalse(lifecycle.health()["connected"])
        self.assertNotIn("secret", json.dumps(lifecycle.health()))
        self.assertEqual(len(clients[0].calls), 1)
        self.assertTrue(lifecycle.close())

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
        _, lifecycle, clients = self.build()
        lifecycle.start()
        self.wait(lambda: lifecycle.health()["connected"])
        clients[0].disconnect_failure = True
        self.assertFalse(lifecycle.close(timeout=2))
        health = lifecycle.health()
        self.assertFalse(health["lifecycle"]["thread_alive"])
        self.assertTrue(clients[0].loop.is_closed())
        self.assertEqual(health["lifecycle"]["error"]["code"], "broker_disconnect_failed")
        self.assertTrue(health["health_stale"])
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
                def host(args, *, extension, agent_name):
                    extension_seen.append(extension)
                    self.wait(lambda: extension.broker_health()["connected"])
                    self.assertEqual(agent_name, "trading-agent")
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
