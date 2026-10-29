"""All broker clients are in-memory fakes; never connect to an account/Gateway."""

import asyncio
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from yolo_trading.ibkr import IBKRAdapter
from yolo_trading.ibkr_config import IBKRConfig, IBKRConfigurationError, load_ibkr_config


class Event:
    def __init__(self):
        self.handlers = []

    def connect(self, handler):
        self.handlers.append(handler)

    def disconnect(self, handler):
        self.handlers.remove(handler)

    def emit(self, *args):
        for handler in tuple(self.handlers):
            handler(*args)


class Client:
    def __init__(self):
        self.connected = False
        self.disconnectedEvent, self.errorEvent = Event(), Event()
        self.calls, self.disconnects = [], 0
        self.accounts = ["DUexample"]
        self.failure = None
        self.started, self.release = asyncio.Event(), None
        self.disconnect_failure = False
        self.read_failure = None
        self.read_release = {}
        self.read_calls = []
        self.completed_orders = []

    async def connectAsync(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        self.connected = True

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return self.accounts

    def disconnect(self):
        self.disconnects += 1
        if self.disconnect_failure:
            raise OSError("sensitive disconnect diagnostic")
        self.connected = False
        self.disconnectedEvent.emit()

    async def _rows(self, name, rows):
        self.read_calls.append(name)
        release = self.read_release.get(name)
        if release is not None:
            await release.wait()
        if self.read_failure == name:
            raise OSError("sensitive reconciliation diagnostic")
        return rows

    async def reqPositionsAsync(self):
        return await self._rows("positions", [])

    async def reqAllOpenOrdersAsync(self):
        return await self._rows("open_orders", [])

    async def reqCompletedOrdersAsync(self, api_only):
        if api_only is not False:
            raise AssertionError("completed-order reconciliation must include all API clients")
        return await self._rows("completed_orders", self.completed_orders)

    async def reqExecutionsAsync(self):
        return await self._rows("executions", [])

    async def accountSummaryAsync(self, account):
        return await self._rows("account_summary", [NS(account=account)])


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Client()
        self.adapter = IBKRAdapter(IBKRConfig("paper", account="DUexample"), client_factory=lambda: self.client)

    async def asyncTearDown(self):
        self.client.disconnect_failure = False
        await self.adapter.disconnect()

    async def test_construction_and_health_never_connect(self):
        self.assertFalse(self.adapter.health()["connection_attempted"])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.adapter.health()["state"], "disconnected")

    async def test_missing_dependency_and_account_are_actionable(self):
        adapter = IBKRAdapter(IBKRConfig("live"))
        with patch("yolo_trading.ibkr.importlib.util.find_spec", return_value=None), \
             patch("yolo_trading.ibkr.importlib.import_module", side_effect=AssertionError("must not import")):
            health = await adapter.connect()
        self.assertEqual(health["state"], "dependency_missing")
        self.assertEqual(len(health["blockers"]), 2)
        self.assertFalse(health["connection_attempted"])
        missing_account = IBKRAdapter(IBKRConfig("paper"), client_factory=lambda: self.fail("must not construct"))
        self.assertEqual((await missing_account.connect())["state"], "configuration_incomplete")

    async def test_sdk_import_failure_is_not_a_gateway_success(self):
        adapter = IBKRAdapter(IBKRConfig("paper", account="DUexample"))
        with patch("yolo_trading.ibkr.importlib.util.find_spec", return_value=object()), \
             patch("yolo_trading.ibkr.importlib.import_module", side_effect=ImportError("private detail")):
            health = await adapter.connect()
        self.assertEqual(health["state"], "dependency_unavailable")
        self.assertFalse(health["connected"])
        self.assertNotIn("private detail", json.dumps(health))

    async def test_connection_uses_exact_parameters_and_never_guesses_environment(self):
        await self.adapter.reconfigure(IBKRConfig("live", port=4002, client_id=25, account="DUexample"))
        health = await self.adapter.connect()
        args, kwargs = self.client.calls[0]
        self.assertEqual(args, ("127.0.0.1", 4002))
        self.assertEqual(kwargs, {"clientId": 25, "timeout": 5.0, "readonly": True,
                                  "account": "DUexample", "raiseSyncErrors": True})
        self.assertEqual(health["configuration"]["environment"], "live")
        self.assertEqual(health["state"], "connected_read_only")
        self.assertFalse(health["environment_verified"])
        self.assertTrue(health["data_complete"])
        self.assertTrue(health["ready"])
        self.assertEqual(health["reconciliation_status"]["phase"], "ready")
        self.assertFalse(health["execution_enabled"])
        self.assertTrue(self.client.RaiseRequestErrors)
        self.assertNotIn("DUexample", json.dumps(health))

    async def test_concurrent_connects_do_not_create_duplicate_connections(self):
        results = await asyncio.gather(self.adapter.connect(), self.adapter.connect())
        self.assertEqual(len(self.client.calls), 1)
        self.assertTrue(all(item["connected"] for item in results))

    async def test_gateway_failure_is_sanitized_and_has_no_fallback(self):
        self.client.failure = OSError("DUexample secret username diagnostic")
        health = await self.adapter.connect()
        self.assertEqual(health["state"], "gateway_unavailable")
        self.assertEqual(len(self.client.calls), 1)
        self.assertFalse(health["connected"])
        self.assertNotIn("secret", json.dumps(health))
        self.assertNotIn("DUexample", json.dumps(health))
        self.assertEqual(self.client.errorEvent.handlers, [])

    async def test_target_account_mismatch_disconnects_without_selecting_another(self):
        self.client.accounts = ["Uother"]
        health = await self.adapter.connect()
        self.assertEqual(health["state"], "account_mismatch")
        self.assertFalse(health["connected"])
        self.assertEqual(self.adapter.config.account, "DUexample")

    async def test_target_account_must_appear_exactly_once(self):
        self.client.accounts = ["DUexample", "DUexample"]
        health = await self.adapter.connect()
        self.assertEqual(health["state"], "account_mismatch")
        self.assertFalse(health["ready"])
        status = health["reconciliation_status"]
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["failure"]["code"], "target_account_not_unique")
        self.assertEqual(status["failure"]["step"], "account")
        self.assertNotIn("DUexample", json.dumps(status))

    async def test_required_read_failure_is_named_and_never_ready(self):
        self.client.read_failure = "executions"
        health = await self.adapter.connect()
        self.assertTrue(health["connected"])
        self.assertFalse(health["ready"])
        self.assertFalse(health["data_complete"])
        self.assertEqual(health["state"], "reconciliation_failed")
        self.assertEqual(health["reconciliation"]["executions"], False)
        failure = health["reconciliation_status"]["failure"]
        self.assertEqual((failure["code"], failure["step"], failure["reason"]),
                         ("reconciliation_read_failed", "executions", "request_failed"))
        self.assertNotIn("sensitive", json.dumps(health))

    async def test_late_reconciliation_result_cannot_restore_retired_generation(self):
        release = asyncio.Event()
        self.client.read_release["positions"] = release
        task = asyncio.create_task(self.adapter.connect())
        await self.client.started.wait()
        await asyncio.sleep(0)
        connecting = self.adapter.health()["reconciliation_status"]
        self.client.errorEvent.emit(-1, 1100, "private")
        self.client.errorEvent.emit(-1, 1102, "restored")
        retired = self.adapter.health()["reconciliation_status"]
        self.assertGreater(retired["generation"], connecting["generation"])
        release.set()
        health = await task
        self.assertEqual(health["state"], "reconciliation_required")
        self.assertFalse(health["ready"])
        self.assertFalse(health["data_complete"])
        self.client.read_release.clear()
        health = await self.adapter.reconcile()
        self.assertTrue(health["ready"])
        self.assertEqual(health["reconciliation_status"]["ready_generation"],
                         health["reconciliation_status"]["generation"])

    async def test_timeout_and_cancellation_clean_up_connection(self):
        self.client.release = asyncio.Event()
        await self.adapter.reconfigure(IBKRConfig("paper", account="DUexample", connect_timeout=0.01))
        health = await self.adapter.connect()
        self.assertEqual(health["state"], "gateway_unavailable")
        self.assertFalse(self.client.connected)
        task = asyncio.create_task(self.adapter.connect())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.adapter.health()["connected"])
        self.assertEqual(self.client.errorEvent.handlers, [])

    async def test_broker_disconnect_and_recovery_do_not_mark_data_ready(self):
        await self.adapter.connect()
        first = self.adapter.health()["reconciliation_status"]["generation"]
        self.client.errorEvent.emit(-1, 1100, "DUexample private diagnostic")
        self.assertEqual(self.adapter.health()["state"], "broker_unavailable")
        self.client.errorEvent.emit(-1, 1101, "restored")
        health = self.adapter.health()
        self.assertEqual(health["state"], "reconciliation_required")
        self.assertFalse(health["data_complete"])
        self.assertFalse(health["execution_enabled"])
        self.assertFalse(health["ready"])
        self.assertGreater(health["reconciliation_status"]["generation"], first)
        self.client.errorEvent.emit(-1, 10197, "private competing username")
        self.assertEqual(self.adapter.health()["state"], "session_conflict")
        self.client.connected = False
        self.client.disconnectedEvent.emit()
        self.assertEqual(self.adapter.health()["state"], "gateway_unavailable")

    async def test_reconfigure_closes_old_connection_and_never_autoconnects(self):
        await self.adapter.connect()
        old_id = self.adapter.config.config_id
        config = load_ibkr_config({"environment": "paper", "account": "DUexample"},
                                  environ={}, overrides={"environment": "live"})
        health = await self.adapter.reconfigure(config)
        self.assertEqual(self.client.disconnects, 1)
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.errorEvent.handlers, [])
        self.assertFalse(health["connection_attempted"])
        self.assertFalse(health["connected"])
        self.assertEqual(health["state"], "configuration_incomplete")
        self.assertEqual(health["configuration_revision"], 2)
        self.assertEqual(health["last_configuration_change"]["from_config_id"], old_id)
        self.assertEqual(health["last_configuration_change"]["to_config_id"], config.config_id)

    async def test_failed_disconnect_blocks_environment_switch(self):
        await self.adapter.connect()
        before = self.adapter.health()
        self.assertTrue(before["ready"])
        self.assertIsNotNone(before["session_id"])
        self.client.disconnect_failure = True
        old = self.adapter.config
        health = await self.adapter.reconfigure(IBKRConfig("live", account="Uexample"))
        self.assertEqual(health["state"], "disconnect_failed")
        self.assertFalse(health["ready"])
        self.assertFalse(health["data_complete"])
        self.assertFalse(health["execution_enabled"])
        self.assertIsNone(health["session_id"])
        self.assertEqual(health["reconciliation_status"]["phase"], "disconnect_failed")
        self.assertGreater(health["reconciliation_status"]["generation"],
                           before["reconciliation_status"]["generation"])
        self.assertIs(self.adapter.config, old)
        self.assertEqual(health["configuration_revision"], 1)
        self.assertEqual(len(self.client.calls), 1)
        self.assertNotIn("sensitive", json.dumps(health))

    async def test_invalid_new_configuration_cannot_mutate_connected_profile(self):
        await self.adapter.connect()
        old = self.adapter.config
        with self.assertRaises(IBKRConfigurationError):
            IBKRConfig("live", execution_enabled=True)
        with self.assertRaises(TypeError):
            await self.adapter.reconfigure({"environment": "live"})
        self.assertIs(self.adapter.config, old)
        self.assertTrue(self.adapter.health()["connected"])


if __name__ == "__main__":
    unittest.main()
