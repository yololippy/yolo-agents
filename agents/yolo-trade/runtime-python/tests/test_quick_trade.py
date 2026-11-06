"""Direct trading uses fake official SDK calls and real local HTTP/SQLite only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_api
import test_ibkr_execution as execution
from yolo_trade.ibkr import IBKROperationError, IBKRAdapter
from yolo_trade.paper_order_store import PaperOrderStore
from yolo_trade.paper_orders import PaperOrderService, OrderError
from yolo_trade.risk_policy import DEFAULT_RISK_POLICY


IDENTITY = {"con_id": 265598, "symbol": "AAPL", "sec_type": "STK", "exchange": "SMART",
            "currency": "USD", "primary_exchange": "NASDAQ"}


class DirectOrderMessageTests(unittest.TestCase):
    def test_confirmation_error_describes_the_authorized_device_boundary(self):
        self.assertEqual(OrderError("confirmation_required").message,
                         "需要同一已授权设备对当前预览作出明确确认。")


class DirectBrokerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = execution.ExecutionTests.asyncSetUp
    asyncTearDown = execution.ExecutionTests.asyncTearDown

    async def test_search_filters_and_caps_and_redacts(self):
        rows = []
        for index in range(20):
            contract = deepcopy(self.client.contract)
            contract.conId += index
            contract.description = "Apple Inc."
            rows.append(NS(contract=contract))
        rows[0].contract.currency = "CAD"
        rows[1].contract.secType = "OPT"
        rows[2].contract.primaryExchange = ""
        rows[3].contract.conId = True
        rows[4].contract.description = "DU123456 private text"
        async def matching(query):
            self.client.touch()
            self.assertEqual(query, "Apple")
            return rows
        self.client.reqMatchingSymbolsAsync = matching
        result = await self.broker.search_instruments("Apple")
        self.assertEqual(result["count"], 12)
        self.assertIsNone(result["instruments"][0]["name"])
        self.assertEqual(result["instruments"][1]["exchange"], "SMART")
        self.assertNotIn("DU123456", json.dumps(result))
        self.assertTrue(all(row["sec_type"] == "STK" and row["currency"] == "USD" for row in result["instruments"]))

    async def test_concurrent_search_calls_are_spaced_at_least_one_second(self):
        calls = []
        async def matching(query):
            calls.append(asyncio.get_running_loop().time())
            return []
        self.client.reqMatchingSymbolsAsync = matching
        await asyncio.gather(self.broker.search_instruments("A"), self.broker.search_instruments("B"))
        self.assertGreaterEqual(calls[1] - calls[0], 1)

    async def test_search_error_timeout_and_throttle_are_safe(self):
        async def matching(query):
            raise OSError("DU123456 broker secret")
        self.client.reqMatchingSymbolsAsync = matching
        with self.assertRaises(IBKROperationError) as caught:
            await self.broker.search_instruments("A")
        self.assertEqual(caught.exception.code, "search_unavailable")
        self.assertNotIn("DU123456", str(caught.exception))
        with self.assertRaises(IBKROperationError) as caught:
            await self.broker.search_instruments("A", timeout=.01)
        self.assertEqual(caught.exception.code, "search_throttled")
        self.broker._last_symbol_search = None
        async def slow(query):
            await asyncio.sleep(1)
        self.client.reqMatchingSymbolsAsync = slow
        with self.assertRaises(IBKROperationError):
            await self.broker.search_instruments("A", timeout=.01)

    async def test_exact_quote_and_risk_use_con_id_and_reject_different_contract(self):
        quote = await self.broker.instrument_quote(265598)
        self.assertEqual(quote["primary_exchange"], "NASDAQ")
        self.assertEqual(self.client.qualifications[-1].conId, 265598)
        self.request["contract"] = deepcopy(IDENTITY)
        facts = await self.broker.risk_snapshot(self.request)
        self.assertEqual(facts["quote"]["con_id"], 265598)
        self.client.contract.conId += 1
        with self.assertRaises(IBKROperationError):
            await self.broker.instrument_quote(265598)
        self.assertEqual(len(self.client.market_requests), 2)

    async def test_what_if_and_submit_bind_every_exact_identity_field(self):
        self.request["contract"] = deepcopy(IDENTITY)
        preview = await self.broker.preview_paper_order(self.request, order_ref="exact", context=self.ctx)
        self.assertEqual(preview["qualified_contract"], IDENTITY)
        for field, value in (("con_id", 12), ("primary_exchange", "NYSE"), ("symbol", "MSFT")):
            changed = deepcopy(self.request)
            changed["contract"][field] = value
            with self.assertRaises(IBKROperationError):
                await self.broker.submit_paper_order(changed, order_ref="exact", preview=preview, context=self.ctx)
        self.assertFalse(self.client.placed)
        result = await self.broker.submit_paper_order(self.request, order_ref="exact", preview=preview, context=self.ctx)
        self.assertEqual(result["status"], "working")
        self.assertEqual(len(self.client.placed), 1)

    async def test_what_if_rejects_wrong_primary_exchange_before_call(self):
        self.request["contract"] = {**IDENTITY, "primary_exchange": "NYSE"}
        with self.assertRaises(IBKROperationError):
            await self.broker.preview_paper_order(self.request, order_ref="exact", context=self.ctx)
        self.assertFalse(self.client.whatifs)

    async def test_positions_portfolio_scope_and_missing_nan_fail_closed(self):
        own = NS(account=self.config.account, contract=self.client.contract, position=2, avgCost=100)
        self.client.positions = [own, NS(account="DU654321", contract=self.client.contract, position=999, avgCost=1)]
        self.client.portfolio = lambda account: [
            NS(account=self.config.account, contract=self.client.contract, marketPrice=100, marketValue=200,
               unrealizedPNL=5, realizedPNL=0),
            NS(account="DU654321", contract=self.client.contract, marketPrice=9999, marketValue=9999)]
        result = await self.broker.positions_detail()
        self.assertEqual(result["positions"][0]["market_value"], "200")
        self.assertNotIn(self.config.account, json.dumps(result))
        for value in (None, float("nan")):
            own.position = value
            with self.assertRaises(IBKROperationError):
                await self.broker.positions_detail()
        own.position = 2
        async def slow():
            await asyncio.sleep(1)
        self.client.reqPositionsAsync = slow
        with self.assertRaises(IBKROperationError):
            await self.broker.positions_detail(timeout=.01)

    async def test_target_account_rechecked_after_read(self):
        async def matching(query):
            self.client.accounts = ["DU654321"]
            return []
        self.client.reqMatchingSymbolsAsync = matching
        with self.assertRaises(IBKROperationError) as caught:
            await self.broker.search_instruments("A")
        self.assertEqual(caught.exception.code, "account_mismatch")

    async def test_duplicate_target_account_blocks_final_submission_before_place(self):
        self.request["contract"] = deepcopy(IDENTITY)
        preview = await self.broker.preview_paper_order(self.request, order_ref="duplicate-account", context=self.ctx)
        self.client.accounts = [self.config.account, self.config.account]
        with self.assertRaises(IBKROperationError) as caught:
            await self.broker.submit_paper_order(self.request, order_ref="duplicate-account", preview=preview, context=self.ctx)
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertFalse(self.client.placed)

    async def test_exact_recovery_accepts_new_contract_schema(self):
        self.request["contract"] = deepcopy(IDENTITY)
        plan = execution.recovery_plan(self.config, str(uuid4()), request=self.request)
        broker = IBKRAdapter(self.config, contract_factory=NS, order_factory=NS)
        result = await broker.restore_tracked_orders(plan)
        self.assertEqual(result["restored"], 1)


class DirectAPITests(unittest.TestCase):
    setUp = test_api.PaperOrderAPITests.setUp
    request = test_api.HTTPTests.request
    action_data = test_api.PaperOrderAPITests.action_data
    action = test_api.PaperOrderAPITests.action
    create = test_api.PaperOrderAPITests.create
    previewed = test_api.PaperOrderAPITests.previewed
    confirmed = test_api.PaperOrderAPITests.confirmed

    def draft_data(self):
        data = test_api.PaperOrderAPITests.draft_data(self)
        data["contract"] = deepcopy(IDENTITY)
        return data

    def authorize_phone(self, enabled=True, token=None):
        return self.request("POST", "/v1/paper-trading/devices/" + self.ios["device"]["id"],
                            {"enabled": enabled}, token or self.token)

    def exact_risk(self):
        original = self.broker.risk_snapshot
        def read(intent):
            result = original(intent)
            result["quote"].update(intent["contract"])
            return result
        self.broker.risk_snapshot = read

    def ios_flow(self, until="submit"):
        token = self.ios["token"]
        status, result = self.request("POST", "/v1/orders", self.draft_data(), token)
        self.assertEqual(status, 201, result)
        order = result["order"]
        for action in ("preview", "confirm", "submit", "cancel"):
            status, result = self.action(order, action, token=token)
            self.assertEqual(status, 200, result)
            order = result["order"]
            if action == until:
                return order

    def test_ios_authorization_complete_direct_flow_without_model(self):
        self.exact_risk()
        self.assertEqual(self.authorize_phone()[0], 200)
        with patch.object(self.app.runtime, "complete", side_effect=AssertionError("model called"), create=True):
            order = self.ios_flow("cancel")
        self.assertEqual(order["state"], "cancelled")
        self.assertEqual(self.broker.calls, {"preview": 1, "submit": 1, "cancel": 1})
        self.assertEqual(order["contract"], IDENTITY)
        self.assertNotIn("DU_SECRET_ACCOUNT", json.dumps(order))

    def test_authorization_revision_responses_and_frozen_clock_changes(self):
        status, result = self.request("GET", "/v1/paper-trading/authorization", token=self.ios["token"])
        self.assertEqual(status, 200)
        self.assertEqual(result["authorization"], {"device_id": self.ios["device"]["id"],
            "paper_trading_enabled": False, "authorization_revision": None})
        service = self.extension._orders
        frozen = datetime.now(timezone.utc)
        service.clock = lambda: frozen
        revisions = []
        for enabled in (True, False, True):
            status, result = self.authorize_phone(enabled)
            self.assertEqual(status, 200, result)
            authorization = result["authorization"]
            revision = authorization["authorization_revision"]
            self.assertIsInstance(revision, str)
            revisions.append(revision)
            current = self.request("GET", "/v1/paper-trading/authorization", token=self.ios["token"])[1]["authorization"]
            listed = self.request("GET", "/v1/paper-trading/devices", token=self.token)[1]["devices"][0]
            self.assertEqual(current, authorization)
            self.assertEqual(listed["authorization_revision"], revision)
            self.assertEqual(listed["paper_trading_enabled"], enabled)
            with service.store.read_only() as db:
                self.assertEqual(db.execute("SELECT granted_at FROM paper_device_grants").fetchone()[0], revision)
        self.assertEqual(len(set(revisions)), 3)
        mac = self.request("GET", "/v1/paper-trading/authorization", token=self.token)[1]["authorization"]
        self.assertEqual(mac, {"device_id": self.mac["device"]["id"],
                              "paper_trading_enabled": True, "authorization_revision": None})

    def test_old_ios_confirmation_cannot_submit_after_revoke_and_regrant(self):
        self.exact_risk()
        self.request("GET", "/v1/orders", token=self.token)
        service = self.extension._orders
        frozen = datetime.now(timezone.utc)
        service.clock = lambda: frozen
        initial = self.authorize_phone()[1]["authorization"]["authorization_revision"]
        order = self.ios_flow("confirm")
        self.assertNotIn("authorization_revision", order["confirmation"])
        with service.store.read_only() as db:
            saved = json.loads(db.execute("SELECT confirmation FROM paper_orders WHERE id=?", (order["id"],)).fetchone()[0])
            self.assertEqual(saved["authorization_revision"], initial)
        self.authorize_phone(False)
        current = self.authorize_phone()[1]["authorization"]["authorization_revision"]
        self.assertNotEqual(current, initial)
        status, result = self.action(order, "submit", token=self.ios["token"])
        self.assertEqual((status, result["error"]["code"]), (403, "permission_revoked"))
        self.assertEqual(self.broker.calls["submit"], 0)
        with service.store.read_only() as db:
            self.assertFalse(db.execute("SELECT 1 FROM paper_reservations").fetchone())
        # A new review under the current grant can still complete normally.
        self.assertEqual(self.ios_flow("cancel")["state"], "cancelled")

    def test_regrant_during_submit_risk_check_invalidates_confirmation(self):
        self.exact_risk()
        self.authorize_phone()
        order = self.ios_flow("confirm")
        read = self.broker.risk_snapshot
        def regrant(intent):
            self.authorize_phone(False)
            self.authorize_phone()
            return read(intent)
        self.broker.risk_snapshot = regrant
        status, result = self.action(order, "submit", token=self.ios["token"])
        self.assertEqual((status, result["error"]["code"]), (403, "permission_revoked"))
        self.assertEqual(self.broker.calls["submit"], 0)
        with self.extension._orders.store.read_only() as db:
            self.assertFalse(db.execute("SELECT 1 FROM paper_reservations").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM paper_submission_guard").fetchone())

    def test_legacy_ios_confirmation_without_revision_fails_closed(self):
        self.exact_risk()
        self.authorize_phone()
        order = self.ios_flow("confirm")
        with self.extension._orders.store.transaction() as db:
            saved = json.loads(db.execute("SELECT confirmation FROM paper_orders WHERE id=?", (order["id"],)).fetchone()[0])
            del saved["authorization_revision"]
            db.execute("UPDATE paper_orders SET confirmation=? WHERE id=?", (json.dumps(saved), order["id"]))
        status, result = self.action(order, "submit", token=self.ios["token"])
        self.assertEqual((status, result["error"]["code"]), (403, "permission_revoked"))
        self.assertEqual(self.broker.calls["submit"], 0)

    def test_grant_requires_mac_and_valid_paired_ios_and_is_persistent(self):
        self.assertEqual(self.authorize_phone(token=self.ios["token"])[0], 403)
        self.assertEqual(self.request("POST", "/v1/paper-trading/devices/" + str(uuid4()), {"enabled": True}, self.token)[0], 400)
        self.assertEqual(self.request("POST", "/v1/paper-trading/devices/" + self.mac["device"]["id"], {"enabled": True}, self.token)[0], 400)
        self.assertEqual(self.authorize_phone()[0], 200)
        service = PaperOrderService(PaperOrderStore(self.path), self.broker)
        service.require_trader(self.ios["device"])
        status, result = self.request("GET", "/v1/paper-trading/devices", token=self.token)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["devices"][0]["paper_trading_enabled"])
        self.assertEqual(self.authorize_phone(False)[0], 200)
        with self.assertRaises(OrderError):
            service.require_trader(self.ios["device"])

    def test_unauthorized_forged_and_revoked_phone_rejected_every_action(self):
        self.exact_risk()
        for action in ("create", "preview", "confirm", "submit", "cancel"):
            if action == "create":
                status, _ = self.request("POST", "/v1/orders", self.draft_data(), self.ios["token"])
            else:
                status, _ = self.action(self.create(), action, data={"client_request_id": str(uuid4())}, token=self.ios["token"])
            self.assertEqual(status, 403)
        self.authorize_phone()
        order = self.ios_flow("confirm")
        self.authorize_phone(False)
        self.assertEqual(self.action(order, "submit", token=self.ios["token"])[0], 403)
        self.authorize_phone()
        self.auth.revoke(self.mac["device"], self.ios["device"]["id"])
        self.assertEqual(self.action(order, "submit", token=self.ios["token"])[0], 401)
        self.assertEqual(self.broker.calls["submit"], 0)

    def test_confirmation_bound_to_same_device_and_exact_contract_required(self):
        self.exact_risk()
        self.authorize_phone()
        order = self.ios_flow("confirm")
        self.assertEqual(self.action(order, "submit")[0], 409)
        old = test_api.PaperOrderAPITests.draft_data(self)
        self.assertEqual(self.request("POST", "/v1/orders", old, self.ios["token"])[0], 400)
        for contract in ({**IDENTITY, "con_id": True}, {**IDENTITY, "primary_exchange": ""},
                         {**IDENTITY, "currency": "CAD"}, {**IDENTITY, "sec_type": "OPT"}):
            data = self.draft_data()
            data["contract"] = contract
            self.assertEqual(self.request("POST", "/v1/orders", data, self.ios["token"])[0], 400)

    def test_revoke_during_final_risk_read_prevents_submit_and_releases_reservation(self):
        self.exact_risk()
        self.authorize_phone()
        order = self.ios_flow("confirm")
        original = self.broker.risk_snapshot
        def revoke(intent):
            self.authorize_phone(False)
            return original(intent)
        self.broker.risk_snapshot = revoke
        status, result = self.action(order, "submit", token=self.ios["token"])
        self.assertEqual(status, 403, result)
        self.assertEqual(self.broker.calls["submit"], 0)
        with self.extension._orders.store.read_only() as db:
            self.assertFalse(db.execute("SELECT 1 FROM paper_reservations").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM paper_submission_guard").fetchone())

    def test_account_submission_serializes_distinct_orders_and_preserves_unknown_on_restart(self):
        self.exact_risk()
        self.authorize_phone()
        first = self.ios_flow("confirm")
        data = self.draft_data()
        data["quantity"] = 3
        second = self.action(self.action(self.create(data), "preview")[1]["order"], "confirm")[1]["order"]
        entered, release = threading.Event(), threading.Event()
        def block(ref):
            entered.set()
            release.wait(2)
        self.broker.submit_hook = block
        self.broker.submit_error = TimeoutError("DU123456 secret")
        submit_data = self.action_data(first, "submit")
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self.action, first, "submit", submit_data, self.ios["token"])
            self.assertTrue(entered.wait(1))
            status, result = self.action(second, "submit")
            self.assertEqual(status, 409, result)
            self.assertEqual(result["error"]["code"], "submission_in_progress")
            release.set()
            status, result = future.result()
        self.assertEqual(result["order"]["state"], "unknown")
        replay = self.action(first, "submit", submit_data, self.ios["token"])
        self.assertEqual(replay, (status, result))
        service = PaperOrderService(PaperOrderStore(self.path), self.broker)
        service.recover_interrupted()
        status, result = service.operate("submit", self.action_data(second, "submit"), self.mac["device"], self.auth.server_id,
                                        order_id=second["id"], authorize=lambda: self.auth.reauthorize(self.mac["device"]))
        self.assertEqual((status, result["error"]["code"]), (409, "submission_in_progress"))
        self.assertEqual(self.broker.calls["submit"], 1)

    def test_persistent_pending_exposure_counts_when_broker_snapshot_lags(self):
        self.exact_risk()
        self.extension.risk_policy = replace(DEFAULT_RISK_POLICY, max_daily_submitted_notional_usd="45", monetary_limit_exempt_symbols=())
        first = self.confirmed()
        data = self.draft_data()
        data["quantity"] = 3
        draft = self.create(data)
        second = self.action(self.action(draft, "preview")[1]["order"], "confirm")[1]["order"]
        self.assertEqual(self.action(first, "submit")[0], 200)
        status, result = self.action(second, "submit")
        self.assertEqual((status, result["error"]["code"]), (409, "risk_daily_limit"))
        self.assertEqual(self.broker.calls["submit"], 1)

    def test_reserved_exposure_remains_when_current_broker_view_is_empty(self):
        self.exact_risk()
        first = self.confirmed()
        self.action(first, "submit")
        data = self.draft_data()
        data["quantity"] = 3
        proposed = self.action(self.create(data), "preview")[1]["order"]
        self.assertEqual(proposed["preview"]["risk"]["metrics"]["projected_gross_exposure_usd"], "50")

    def test_filled_releases_open_exposure_but_preserves_daily_submitted_notional(self):
        self.exact_risk()
        self.extension.risk_policy = replace(DEFAULT_RISK_POLICY, monetary_limit_exempt_symbols=())
        self.broker.submit_result.update(status="filled", filled_quantity=2, average_fill_price="10")
        filled = self.action(self.confirmed(), "submit")[1]["order"]
        self.assertEqual(filled["state"], "filled")
        service = self.extension._orders
        with service.store.read_only() as db:
            self.assertFalse(db.execute("SELECT 1 FROM paper_reservations").fetchone())
        read = self.broker.risk_snapshot
        def current_positions(intent):
            facts = read(intent)
            facts["account"]["gross_position_value"] = "20"
            facts["positions"].update(positions=[{**IDENTITY, "quantity": "2"}], count=1)
            return facts
        self.broker.risk_snapshot = current_positions
        data = self.draft_data()
        data["quantity"] = 3
        proposed = self.action(self.create(data), "preview")[1]["order"]
        metrics = proposed["preview"]["risk"]["metrics"]
        self.assertEqual(metrics["projected_gross_exposure_usd"], "50")
        self.assertEqual(metrics["daily_submitted_notional_usd"], "50")
        self.assertEqual(service._daily_submitted_notional(proposed["id"]), "20")

    def test_terminal_updates_release_reservations_and_keep_daily_history(self):
        self.exact_risk()
        self.extension.risk_policy = replace(DEFAULT_RISK_POLICY, monetary_limit_exempt_symbols=())
        for index, state in enumerate(("filled", "rejected", "cancelled")):
            with self.subTest(state=state):
                order = self.action(self.confirmed(), "submit")[1]["order"]
                service = self.extension._orders
                with service.store.read_only() as db:
                    self.assertIsNotNone(db.execute("SELECT 1 FROM paper_reservations WHERE order_id=?", (order["id"],)).fetchone())
                update = {"order_ref": order["id"], "status": state, "broker_order_id": "123",
                          "filled_quantity": 2 if state == "filled" else 0,
                          "average_fill_price": "10" if state == "filled" else None}
                if state == "filled":
                    self.broker.updates = [{**update, "config_id": self.broker.config_id, "session_id": "session-1"}]
                    service.refresh_cached_updates()
                    self.broker.updates = []
                else:
                    service.apply_broker_update(order["id"], update)
                with service.store.read_only() as db:
                    self.assertFalse(db.execute("SELECT 1 FROM paper_reservations").fetchone())
                self.assertEqual(service._daily_submitted_notional("new-order"), str(20 * (index + 1)))

    def test_nonterminal_states_keep_reservations(self):
        self.exact_risk()
        for state in ("accepted", "working", "partial", "unknown"):
            with self.subTest(state=state):
                self.broker.submit_result.update(status=state, filled_quantity=1 if state == "partial" else 0)
                order = self.action(self.confirmed(), "submit")[1]["order"]
                service = self.extension._orders
                with service.store.read_only() as db:
                    self.assertIsNotNone(db.execute("SELECT 1 FROM paper_reservations WHERE order_id=?", (order["id"],)).fetchone())
                service.apply_broker_update(order["id"], {"order_ref": order["id"], "status": "cancelled",
                    "broker_order_id": "123", "filled_quantity": order["filled_quantity"]})

    def test_restart_removes_legacy_terminal_reservations_without_losing_daily_history(self):
        self.exact_risk()
        self.extension.risk_policy = replace(DEFAULT_RISK_POLICY, monetary_limit_exempt_symbols=())
        self.broker.submit_result.update(status="filled", filled_quantity=2, average_fill_price="10")
        filled = self.action(self.confirmed(), "submit")[1]["order"]
        service = self.extension._orders
        with service.store.transaction() as db:
            db.execute("INSERT INTO paper_reservations VALUES (?,?,?,?)", (filled["id"], self.broker.config_id, "20", service.now()))
        service.recover_interrupted()
        with service.store.read_only() as db:
            self.assertFalse(db.execute("SELECT 1 FROM paper_reservations").fetchone())
        self.assertEqual(service._daily_submitted_notional("new-order"), "20")

    def test_broker_id_collision_cannot_hide_local_reservation(self):
        self.exact_risk()
        first = self.confirmed()
        self.action(first, "submit")
        service = self.extension._orders
        facts = self.broker.risk_snapshot(self.draft_data())
        facts["open_orders"]["orders"] = [{"broker_order_id": "123", "order_ref": str(uuid4())}]
        result = service._reserved_facts(facts, "different-order")
        self.assertEqual(len(result["open_orders"]["orders"]), 2)
        facts["open_orders"]["orders"][0]["order_ref"] = first["id"]
        result = service._reserved_facts(facts, "different-order")
        self.assertEqual(len(result["open_orders"]["orders"]), 1)

    def test_forged_platform_and_missing_reauthentication_do_not_use_ios_grant(self):
        self.authorize_phone()
        self.request("GET", "/v1/orders", token=self.token)
        service = self.extension._orders
        forged = {**self.ios["device"], "platform": "macos", "permissions": ["devices:manage"]}
        with self.assertRaises(OrderError):
            service.operate("create", self.draft_data(), forged, self.auth.server_id,
                            authorize=lambda: self.auth.reauthorize(self.ios["device"]))
        with self.assertRaises(OrderError):
            service.operate("create", self.draft_data(), self.ios["device"], self.auth.server_id)

    def test_failed_submit_risk_releases_budget_and_retry_remains_explicit(self):
        self.exact_risk()
        order = self.confirmed()
        read = self.broker.risk_snapshot
        self.broker.risk_snapshot = lambda intent: {}
        status, _ = self.action(order, "submit")
        self.assertEqual(status, 409)
        with self.extension._orders.store.read_only() as db:
            self.assertFalse(db.execute("SELECT 1 FROM paper_reservations").fetchone())
        self.broker.risk_snapshot = read
        self.assertEqual(self.action(order, "submit")[0], 200)
        self.assertEqual(self.broker.calls["submit"], 1)

    def test_direct_reads_auth_strict_query_paths_response_and_error_redaction(self):
        now = datetime.now(timezone.utc).isoformat()
        self.broker.search_instruments = lambda query: {"instruments": [{**IDENTITY, "name": "Apple Inc.", "account": "DU_SECRET_ACCOUNT"}], "count": 1, "as_of": now}
        self.broker.instrument_quote = lambda con_id: {**IDENTITY, "bid": "99", "ask": "100", "last": None, "close": None,
                                                       "data_type": "delayed", "as_of": now, "account": "DU_SECRET_ACCOUNT"}
        self.broker.positions_detail = lambda: {"positions": [{**IDENTITY, "quantity": "2", "average_cost": "100"}], "count": 1, "as_of": now}
        for path in ("/v1/positions", "/v1/instruments/search?q=Apple", "/v1/instruments/265598/quote"):
            self.assertEqual(self.request("GET", path)[0], 401)
            status, result = self.request("GET", path, token=self.ios["token"])
            self.assertEqual(status, 200, result)
            self.assertNotIn("DU_SECRET_ACCOUNT", json.dumps(result))
        for path in ("/v1/positions/", "/v1/positions?&", "/v1/positions?x=", "/v1/instruments/search?q=", "/v1/instruments/search?q=A&q=B",
                     "/v1/instruments/search?q=%20", "/v1/instruments/search?q=A&unknown=", "/v1/instruments/0/quote",
                     "/v1/instruments/0265598/quote", "/v1/instruments/2147483648/quote", "/v1/instruments/265598/quote?x="):
            self.assertEqual(self.request("GET", path, token=self.token)[0], 400, path)
        self.assertEqual(self.request("GET", "/v1/positions", {}, self.token)[0], 400)
        self.broker.instrument_quote = lambda con_id: {**IDENTITY, "bid": "NaN", "as_of": now}
        self.assertEqual(self.request("GET", "/v1/instruments/265598/quote", token=self.token)[0], 409)
        def fail(query):
            raise OSError("DU123456 broker secret")
        self.broker.search_instruments = fail
        status, result = self.request("GET", "/v1/instruments/search?q=A", token=self.token)
        self.assertEqual(status, 409)
        self.assertNotIn("DU123456", json.dumps(result))

    def test_ios_cannot_enable_live_or_supply_a_limit_from_quote_implicitly(self):
        self.authorize_phone()
        data = self.draft_data()
        data["environment"] = "live"
        self.assertEqual(self.request("POST", "/v1/orders", data, self.ios["token"])[0], 403)
        data = self.draft_data()
        del data["limit_price"]
        self.assertEqual(self.request("POST", "/v1/orders", data, self.ios["token"])[0], 400)


class DirectLifecycleTests(unittest.TestCase):
    build = execution.ExecutionLifecycleTests.build
    wait = execution.ExecutionLifecycleTests.wait

    def test_search_positions_and_exact_quote_stay_on_owner_loop(self):
        broker, client, _ = self.build()
        async def matching(query):
            client.touch()
            return [NS(contract=client.contract)]
        client.reqMatchingSymbolsAsync = matching
        self.assertEqual(broker.search_instruments("Apple")["count"], 1)
        self.assertEqual(broker.instrument_quote(265598)["con_id"], 265598)
        self.assertEqual(broker.positions_detail()["count"], 0)
        self.assertNotEqual(client.owner, threading.get_ident())
        self.assertEqual(set(client.access_threads), {client.owner})


if __name__ == "__main__":
    unittest.main()
