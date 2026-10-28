"""Paper execution tests use only injected in-memory SDK types and clients."""

import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

from test_ibkr import Client, Event
from yolo_trading.ibkr import IBKRAdapter, IBKROperationError
from yolo_trading.ibkr_config import IBKRConfig
from yolo_trading.ibkr_lifecycle import IBKRBrokerLifecycle


def paper_config(**overrides):
    return IBKRConfig(**{"environment": "paper", "account": "DU123456", "execution_enabled": True,
                         "read_only": False, **overrides})


def intent(config, **overrides):
    return {"account_selection": config.config_id,
            "contract": {"symbol": "AAPL", "sec_type": "STK", "exchange": "SMART", "currency": "USD"},
            "side": "BUY", "quantity": 10, "limit_price": "100.00", "tif": "DAY", "order_type": "LMT", **overrides}


def context(broker):
    value = broker.paper_order_snapshot()
    return {key: value[key] for key in ("config_id", "session_id")}


class ExecutionClient(Client):
    def __init__(self):
        super().__init__()
        self.owner = threading.get_ident()
        self.loop = asyncio.get_running_loop()
        self.access_threads = []
        self.accounts = ["DU123456", "DU654321"]
        for name in ("openOrderEvent", "orderStatusEvent", "execDetailsEvent", "commissionReportEvent"):
            setattr(self, name, Event())
        self.client = NS(getReqId=self.next_id)
        self.next_order_id = 100
        self.contract = NS(conId=265598, symbol="AAPL", secType="STK", exchange="SMART", currency="USD",
                           localSymbol="AAPL", primaryExchange="NASDAQ", tradingClass="NMS")
        self.details = [NS(contract=self.contract)]
        self.state = NS(initMarginChange="1000.00", maintMarginChange="500.00", commission="1.00",
                        commissionCurrency="USD", warningText="", status="PreSubmitted")
        self.read_failure = None
        self.place_mode = "ack"
        self.open_orders, self.execution_rows = [], []
        self.placed, self.cancelled, self.whatifs, self.qualifications = [], [], [], []

    def touch(self):
        self.access_threads.append(threading.get_ident())
        if threading.get_ident() != self.owner:
            raise AssertionError("SDK touched from foreign thread")

    async def connectAsync(self, *args, **kwargs):
        self.touch()
        await super().connectAsync(*args, **kwargs)

    def isConnected(self):
        self.touch()
        return super().isConnected()

    def managedAccounts(self):
        self.touch()
        return super().managedAccounts()

    def disconnect(self):
        self.touch()
        super().disconnect()

    async def rows(self, name, result):
        self.touch()
        self.read_calls.append(name)
        if self.read_failure == name:
            raise OSError("DU123456 secret broker text")
        return result

    async def reqPositionsAsync(self):
        return await self.rows("positions", [])

    async def reqAllOpenOrdersAsync(self):
        return await self.rows("open_orders", self.open_orders)

    async def reqExecutionsAsync(self):
        return await self.rows("executions", self.execution_rows)

    async def accountSummaryAsync(self, account):
        return await self.rows("account_summary", [NS(account=account, tag="BuyingPower", value="100000", currency="USD")])

    async def reqContractDetailsAsync(self, query):
        self.touch()
        self.qualifications.append(query)
        return self.details

    async def whatIfOrderAsync(self, contract, order):
        self.touch()
        self.whatifs.append((contract, order))
        return self.state

    def next_id(self):
        self.touch()
        result = self.next_order_id
        self.next_order_id += 1
        return result

    def placeOrder(self, contract, order):
        self.touch()
        order.permId = 4000 + order.orderId
        trade = NS(contract=contract, order=order, orderStatus=NS(status="PendingSubmit", filled=0, avgFillPrice=0))
        self.placed.append(trade)
        if self.place_mode == "throw_after_write":
            raise OSError("sensitive broker message")
        if self.place_mode == "missing_return":
            return None
        if self.place_mode == "ack":
            self.loop.call_soon(self.ack, trade)
        elif self.place_mode == "reject":
            self.loop.call_soon(self.errorEvent.emit, order.orderId, 201, "DU123456 secret rejection")
        return trade

    def ack(self, trade):
        trade.orderStatus.status = "Submitted"
        self.orderStatusEvent.emit(trade)

    def cancelOrder(self, order):
        self.touch()
        self.cancelled.append(order)
        trade = next(trade for trade in self.placed if trade.order is order)
        trade.orderStatus.status = "PendingCancel"
        return trade

    def fill(self, trade, exec_id, quantity, price="100"):
        return NS(contract=deepcopy(trade.contract), execution=NS(acctNumber=trade.order.account, clientId=trade.order.clientId,
                              orderId=trade.order.orderId, permId=trade.order.permId,
                              execId=exec_id, shares=Decimal(quantity), price=Decimal(price)))


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = ExecutionClient()
        self.config = paper_config()
        self.broker = IBKRAdapter(self.config, client_factory=lambda: self.client,
                                  contract_factory=NS, order_factory=NS)
        await self.broker.connect()
        self.ctx, self.request = context(self.broker), intent(self.config)

    async def asyncTearDown(self):
        await self.broker.disconnect()

    async def preview(self, ref="intent-1", request=None):
        return await self.broker.preview_paper_order(request or self.request, order_ref=ref, context=self.ctx)

    async def submit(self, ref="intent-1", request=None, timeout=0.03):
        request = request or self.request
        preview = await self.preview(ref, request)
        return await self.broker.submit_paper_order(request, order_ref=ref, preview=preview, context=self.ctx, timeout=timeout)

    async def test_exact_target_writable_paper_and_full_initial_reads(self):
        value = self.broker.paper_order_snapshot()
        self.assertTrue(all(value[key] for key in ("environment_verified", "account_verified", "permissions_verified",
                                                 "execution_enabled", "connected", "data_complete")))
        self.assertFalse(value["read_only"])
        self.assertEqual(self.client.calls[0][1]["account"], "DU123456")
        self.assertFalse(self.client.calls[0][1]["readonly"])
        self.assertNotIn("DU123456", json.dumps(value))

    async def test_what_if_uses_complete_contract_but_never_submits(self):
        result = await self.preview()
        contract, order = self.client.whatifs[0]
        self.assertEqual(contract.primaryExchange, "NASDAQ")
        self.assertEqual(contract.conId, 265598)
        self.assertEqual((order.orderRef, order.whatIf, order.account), ("intent-1", True, "DU123456"))
        self.assertTrue(order.transmit)
        self.assertEqual(result["what_if"]["commission"], "1.00")
        self.assertEqual(self.client.placed, [])

    async def test_submit_once_binds_order_ref_and_returns_broker_ack(self):
        preview = await self.preview()
        first, second = await asyncio.gather(*[
            self.broker.submit_paper_order(self.request, order_ref="intent-1", preview=preview, context=self.ctx)
            for _ in range(2)])
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "working")
        self.assertEqual(first["broker_order_id"], "100")
        self.assertEqual(first["perm_id"], 4100)
        self.assertEqual(len(self.client.placed), 1)
        trade = self.client.placed[0]
        self.assertEqual((trade.order.orderRef, trade.order.orderId, trade.order.clientId), ("intent-1", 100, 20))
        self.assertFalse(trade.order.whatIf)
        self.assertFalse(trade.order.outsideRth)
        self.assertNotIn("DU123456", json.dumps(self.broker.orders_snapshot()))
        changed = {**self.request, "quantity": 20}
        with self.assertRaisesRegex(IBKROperationError, "同一 intent"):
            await self.broker.submit_paper_order(changed, order_ref="intent-1", preview=preview, context=self.ctx)

    async def test_timeout_missing_return_and_exception_never_resend(self):
        for mode in ("silent", "missing_return", "throw_after_write"):
            with self.subTest(mode=mode):
                self.client.place_mode = mode
                # Earlier unresolved rows intentionally prevent a new intent.
                if self.client.placed:
                    self.client.ack(self.client.placed[-1])
                ref = "intent-" + mode
                preview = await self.preview(ref)
                result = await self.broker.submit_paper_order(self.request, order_ref=ref, preview=preview,
                                                             context=self.ctx, timeout=0.01)
                self.assertEqual(result["status"], "unknown")
                again = await self.broker.submit_paper_order(self.request, order_ref=ref, preview=preview, context=self.ctx)
                self.assertEqual(again, result)
                self.assertEqual(len([trade for trade in self.client.placed if trade.order.orderRef == ref]), 1)
                next_preview = await self.preview(ref + "-next")
                with self.assertRaises(IBKROperationError) as failure:
                    await self.broker.submit_paper_order(self.request, order_ref=ref + "-next", preview=next_preview,
                                                         context=self.ctx)
                self.assertEqual(failure.exception.code, "unresolved_order")

    async def test_rejection_is_not_acknowledged_as_working(self):
        self.client.place_mode = "reject"
        result = await self.submit()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error_code"], 201)
        trade = self.client.placed[0]
        trade.orderStatus.status = "Submitted"
        self.client.orderStatusEvent.emit(trade)
        self.assertEqual(self.broker.orders_snapshot()[0]["status"], "rejected")
        self.assertNotIn("secret", json.dumps(self.broker.events_snapshot()))

    async def test_duplicate_and_out_of_order_status_execution_and_commission(self):
        await self.submit()
        trade = self.client.placed[0]
        fill1 = self.client.fill(trade, "exec-1", "4", "100")
        report = NS(execId="exec-1", commission=Decimal("0.4"), currency="USD")
        self.client.commissionReportEvent.emit(trade, fill1, report)
        self.client.execDetailsEvent.emit(trade, fill1)
        self.client.commissionReportEvent.emit(trade, fill1, report)
        events = self.broker.events_snapshot()
        self.client.commissionReportEvent.emit(trade, fill1, report)
        self.assertEqual(self.broker.events_snapshot(), events)
        trade.orderStatus = NS(status="PreSubmitted", filled=0, avgFillPrice=0)
        self.client.openOrderEvent.emit(trade)
        row = self.broker.orders_snapshot()[0]
        self.assertEqual((row["status"], row["filled_quantity"]), ("partial", 4))
        self.assertEqual(row["executions"]["exec-1"]["commission"], "0.4")
        fill2 = self.client.fill(trade, "exec-2", "6", "102")
        self.client.execDetailsEvent.emit(trade, fill2)
        self.client.orderStatusEvent.emit(trade)
        row = self.broker.orders_snapshot()[0]
        self.assertEqual((row["status"], row["filled_quantity"], row["average_fill_price"]), ("filled", 10, "101.2"))
        self.assertEqual(len(row["executions"]), 2)
        self.assertEqual(self.broker.events_snapshot(after=events[-1]["seq"])[-1]["order"]["status"], "filled")

    async def test_late_full_fill_upgrades_cancelled_without_ordinary_regression(self):
        await self.submit()
        with self.assertRaises(IBKROperationError):
            await self.broker.cancel_paper_order(order_ref="foreign", broker_order_id="100", context=self.ctx)
        with self.assertRaises(IBKROperationError):
            await self.broker.cancel_paper_order(order_ref="intent-1", broker_order_id="999", context=self.ctx)
        result = await self.broker.cancel_paper_order(order_ref="intent-1", broker_order_id="100", context=self.ctx)
        self.assertEqual(result["status"], "cancel_pending")
        await self.broker.cancel_paper_order(order_ref="intent-1", broker_order_id="100", context=self.ctx)
        self.assertEqual(len(self.client.cancelled), 1)
        trade = self.client.placed[0]
        trade.orderStatus.status = "Submitted"
        self.client.orderStatusEvent.emit(trade)
        self.assertEqual(self.broker.orders_snapshot()[0]["status"], "cancel_pending")
        self.client.execDetailsEvent.emit(trade, self.client.fill(trade, "partial-during-cancel", "4"))
        self.assertEqual(self.broker.orders_snapshot()[0]["status"], "cancel_pending")
        trade.orderStatus.status = "Cancelled"
        self.client.orderStatusEvent.emit(trade)
        trade.orderStatus.status = "Submitted"
        self.client.orderStatusEvent.emit(trade)
        self.assertEqual(self.broker.orders_snapshot()[0]["status"], "cancelled")
        self.client.execDetailsEvent.emit(trade, self.client.fill(trade, "late-fill", "6"))
        row = self.broker.orders_snapshot()[0]
        self.assertEqual((row["status"], row["filled_quantity"]), ("filled", 10))

    async def test_foreign_account_client_contract_and_execution_are_ignored(self):
        await self.submit()
        original = self.client.placed[0]
        before = self.broker.orders_snapshot()
        for field, value in (("account", "DU654321"), ("clientId", 55), ("orderRef", "external")):
            trade = deepcopy(original)
            setattr(trade.order, field, value)
            trade.orderStatus = NS(status="Filled", filled=10, avgFillPrice=100)
            self.client.orderStatusEvent.emit(trade)
        trade = deepcopy(original)
        trade.contract.conId = 999
        self.client.orderStatusEvent.emit(trade)
        fill = self.client.fill(original, "foreign-exec", "10")
        fill.execution.acctNumber = "DU654321"
        self.client.execDetailsEvent.emit(original, fill)
        fill = self.client.fill(original, "foreign-contract", "10")
        fill.contract.conId = 999
        self.client.execDetailsEvent.emit(original, fill)
        self.assertEqual(self.broker.orders_snapshot(), before)

    async def test_perm_id_from_status_and_execution_must_remain_consistent(self):
        await self.submit()
        trade = self.client.placed[0]
        trade.orderStatus.permId = trade.order.permId
        trade.order.permId = 0
        self.client.orderStatusEvent.emit(trade)
        self.assertEqual(self.broker.orders_snapshot()[0]["perm_id"], 4100)
        fill = self.client.fill(trade, "conflicting-perm", "4")
        fill.execution.permId = 9999
        self.client.execDetailsEvent.emit(trade, fill)
        self.assertEqual(self.broker.orders_snapshot()[0]["status"], "unknown")
        self.assertEqual(self.broker.orders_snapshot()[0]["filled_quantity"], 0)

    async def test_execution_conflict_after_full_fill_keeps_terminal_state(self):
        await self.submit()
        trade = self.client.placed[0]
        self.client.execDetailsEvent.emit(trade, self.client.fill(trade, "exec-1", "10"))
        self.client.execDetailsEvent.emit(trade, self.client.fill(trade, "exec-1", "10", "101"))
        self.client.orderStatusEvent.emit(trade)
        row = self.broker.orders_snapshot()[0]
        self.assertEqual((row["status"], row["unknown_reason"]), ("filled", "execution_conflict"))

    async def test_reconnect_queries_recover_tracked_order_and_ignore_old_or_foreign_facts(self):
        await self.submit()
        trade = self.client.placed[0]
        old_status_handler = self.client.orderStatusEvent.handlers[0]
        old_session = self.broker.orders_snapshot()[0]["session_id"]
        fill1 = self.client.fill(trade, "recover-1", "4", "100")
        fill1.commissionReport = NS(execId="recover-1", commission=Decimal("0.4"), currency="USD")
        fill2 = self.client.fill(trade, "recover-2", "6", "102")
        fill2.commissionReport = NS(execId="recover-2", commission=Decimal("0.6"), currency="USD")
        foreign = self.client.fill(trade, "foreign-account", "10", "1")
        foreign.execution.acctNumber = "DU654321"

        await self.broker.disconnect()
        self.assertEqual(self.broker.orders_snapshot()[0]["status"], "unknown")
        recovered_trade = deepcopy(trade)
        recovered_trade.orderStatus = NS(status="Submitted", filled=0, avgFillPrice=0,
                                         permId=trade.order.permId)
        self.client.open_orders = [recovered_trade]
        self.client.execution_rows = [fill2, foreign, fill1, fill1]
        before_reads = len(self.client.read_calls)
        await self.broker.connect()

        row = self.broker.orders_snapshot()[0]
        self.assertEqual(self.client.read_calls[before_reads:],
                         ["positions", "open_orders", "executions", "account_summary"])
        self.assertEqual((row["status"], row["filled_quantity"], row["average_fill_price"]),
                         ("filled", 10, "101.2"))
        self.assertEqual(row["executions"]["recover-1"]["commission"], "0.4")
        self.assertEqual(row["executions"]["recover-2"]["commission"], "0.6")
        self.assertNotIn("foreign-account", row["executions"])
        self.assertEqual(row["session_id"], old_session)
        self.assertNotEqual(self.broker.paper_order_snapshot()["session_id"], old_session)
        self.assertEqual(len(self.client.placed), 1)

        stale = deepcopy(trade)
        stale.order.permId = 999999
        old_status_handler(stale)
        after_late = self.broker.orders_snapshot()[0]
        self.assertEqual(after_late, row)
        self.assertNotIn("DU123456", json.dumps(self.broker.events_snapshot()))

    async def test_reconnect_without_order_facts_keeps_unknown_and_never_resends(self):
        await self.submit()
        await self.broker.disconnect()
        self.client.open_orders = []
        self.client.execution_rows = []
        await self.broker.connect()
        self.ctx = context(self.broker)
        self.assertEqual(self.broker.orders_snapshot()[0]["status"], "unknown")
        preview = await self.preview("new-after-reconnect")
        with self.assertRaises(IBKROperationError) as failure:
            await self.broker.submit_paper_order(self.request, order_ref="new-after-reconnect",
                                                 preview=preview, context=self.ctx)
        self.assertEqual(failure.exception.code, "unresolved_order")
        self.assertEqual(len(self.client.placed), 1)

    async def test_contract_details_must_be_unique_and_exact(self):
        for details in ([], self.client.details * 2, [NS(contract=NS(**{**vars(self.client.contract), "conId": 0}))],
                        [NS(contract=NS(**{**vars(self.client.contract), "symbol": "MSFT"}))]):
            self.client.details = details
            with self.assertRaises(IBKROperationError):
                await self.preview()
        self.assertEqual(self.client.whatifs, [])
        self.assertEqual(self.client.placed, [])

    async def test_invalid_order_fields_are_blocked_before_sdk(self):
        cases = [{**self.request, field: value} for field, value in (
            ("quantity", 0), ("quantity", True), ("quantity", 1.5), ("order_type", "MKT"),
            ("tif", "GTC"), ("side", "SSHORT"), ("limit_price", "NaN"), ("limit_price", "0"),
            ("outside_rth", True), ("shortSaleSlot", 1), ("account", "DU654321"))]
        cases += [{**self.request, "contract": {**self.request["contract"], field: value}}
                  for field, value in (("sec_type", "OPT"), ("currency", "HKD"), ("exchange", "NYSE"), ("con_id", 123))]
        for request in cases:
            with self.subTest(request=request), self.assertRaises(IBKROperationError):
                await self.preview(request=request)
        self.assertEqual(self.client.qualifications, [])
        self.assertEqual(self.client.placed, [])

    async def test_sell_regular_stock_limit_order_is_supported(self):
        row = await self.submit(request={**self.request, "side": "SELL"})
        self.assertEqual(row["status"], "working")
        self.assertEqual(self.client.placed[0].order.action, "SELL")
        self.assertEqual(self.client.placed[0].order.shortSaleSlot, 0)

    async def test_incomplete_or_rejected_whatif_is_blocked(self):
        for field, value in (("commission", float("inf")), ("commissionCurrency", "HKD"),
                             ("warningText", "rejected"), ("status", "Inactive"), ("initMarginChange", "")):
            state = deepcopy(self.client.state)
            setattr(self.client.state, field, value)
            with self.subTest(field=field), self.assertRaises(IBKROperationError):
                await self.preview()
            self.client.state = state
        self.assertEqual(self.client.placed, [])

    async def test_preview_binding_tampering_expiry_and_reconnect(self):
        preview = await self.preview()
        altered = deepcopy(preview)
        altered["qualified_contract"]["con_id"] = 123
        with self.assertRaises(IBKROperationError):
            await self.broker.submit_paper_order(self.request, order_ref="intent-1", preview=altered, context=self.ctx)
        self.broker._previews["intent-1"]["expires"] = 0
        with self.assertRaises(IBKROperationError):
            await self.broker.submit_paper_order(self.request, order_ref="intent-1", preview=preview, context=self.ctx)
        await self.broker.disconnect()
        await self.broker.connect()
        with self.assertRaises(IBKROperationError) as failure:
            await self.broker.submit_paper_order(self.request, order_ref="intent-1", preview=preview, context=self.ctx)
        self.assertEqual(failure.exception.code, "stale_broker_context")
        self.assertEqual(self.client.placed, [])

    async def test_failed_repreview_invalidates_previous_success(self):
        preview = await self.preview()
        self.client.state.warningText = "cannot complete"
        with self.assertRaises(IBKROperationError):
            await self.preview()
        with self.assertRaises(IBKROperationError) as failure:
            await self.broker.submit_paper_order(self.request, order_ref="intent-1", preview=preview, context=self.ctx)
        self.assertEqual(failure.exception.code, "preview_required")
        self.assertEqual(self.client.placed, [])

    async def test_old_session_order_cannot_be_cancelled_without_reconciliation(self):
        await self.submit()
        await self.broker.disconnect()
        await self.broker.connect()
        with self.assertRaises(IBKROperationError) as failure:
            await self.broker.cancel_paper_order(order_ref="intent-1", broker_order_id="100", context=context(self.broker))
        self.assertEqual(failure.exception.code, "order_not_owned")
        self.assertEqual(self.client.cancelled, [])

    async def test_connection_loss_stops_execution_and_keeps_unknown(self):
        await self.submit()
        self.client.errorEvent.emit(-1, 1100, "private")
        self.client.errorEvent.emit(-1, 1102, "restored")
        self.assertFalse(self.broker.paper_order_snapshot()["execution_enabled"])
        self.assertFalse(self.broker.paper_order_snapshot()["data_complete"])
        self.assertEqual(self.broker.orders_snapshot()[0]["status"], "unknown")
        with self.assertRaises(IBKROperationError):
            await self.preview("new-intent")

    async def test_each_failed_startup_read_blocks_preview_and_submission(self):
        for read in ("positions", "open_orders", "executions", "account_summary"):
            await self.broker.disconnect()
            self.client.read_failure = read
            await self.broker.connect()
            self.ctx = context(self.broker)
            with self.subTest(read=read):
                value = self.broker.health()
                self.assertFalse(value["data_complete"])
                self.assertFalse(value["execution_enabled"])
                self.assertIn("reconciliation_incomplete", {item["code"] for item in value["blockers"]})
                with self.assertRaises(IBKROperationError):
                    await self.preview()
        self.assertNotIn("secret", json.dumps(self.broker.health()))
        self.assertEqual(self.client.placed, [])

    async def test_missing_required_read_or_invalid_next_valid_id_blocks_send(self):
        await self.broker.disconnect()
        self.client.reqExecutionsAsync = None
        await self.broker.connect()
        self.assertFalse(self.broker.paper_order_snapshot()["data_complete"])
        self.client.reqExecutionsAsync = ExecutionClient.reqExecutionsAsync.__get__(self.client)
        await self.broker.disconnect()
        await self.broker.connect()
        self.ctx = context(self.broker)
        preview = await self.preview()
        self.client.next_order_id = 0
        with self.assertRaises(IBKROperationError) as failure:
            await self.broker.submit_paper_order(self.request, order_ref="intent-1", preview=preview, context=self.ctx)
        self.assertEqual(failure.exception.code, "invalid_order_id")
        self.assertEqual(self.client.placed, [])

    async def test_unsupported_paper_namespace_and_changed_account_block(self):
        await self.broker.disconnect()
        self.client.accounts = ["U123456"]
        config = paper_config(account="U123456")
        await self.broker.reconfigure(config)
        await self.broker.connect()
        self.assertFalse(self.broker.paper_order_snapshot()["environment_verified"])
        with self.assertRaises(IBKROperationError):
            await self.broker.preview_paper_order(intent(config), order_ref="new", context=context(self.broker))
        await self.broker.disconnect()
        self.client.accounts = ["DU123456"]
        await self.broker.reconfigure(self.config)
        await self.broker.connect()
        self.ctx = context(self.broker)
        self.client.accounts = ["DU654321"]
        with self.assertRaises(IBKROperationError) as failure:
            await self.preview()
        self.assertEqual(failure.exception.code, "account_mismatch")

    async def test_dur_paper_namespace_is_supported_but_unknown_du_namespace_is_not(self):
        await self.broker.disconnect()
        for account, verified in (("DUR123456", True), ("DUX123456", False), ("U123456", False)):
            with self.subTest(account=account):
                self.client.accounts = [account]
                config = paper_config(account=account)
                await self.broker.reconfigure(config)
                await self.broker.connect()
                snapshot = self.broker.paper_order_snapshot()
                self.assertEqual(snapshot["environment_verified"], verified)
                self.assertEqual(snapshot["execution_enabled"], verified)
                self.assertNotIn(account, json.dumps(snapshot))
                await self.broker.disconnect()

    async def test_live_and_default_readonly_have_no_execution_path(self):
        await self.broker.disconnect()
        for config in (IBKRConfig("live", account="DU123456"), IBKRConfig("paper", account="DU123456")):
            await self.broker.reconfigure(config)
            await self.broker.connect()
            with self.assertRaises(IBKROperationError) as failure:
                await self.broker.preview_paper_order(intent(config), order_ref="intent", context=context(self.broker))
            self.assertEqual(failure.exception.code, "execution_disabled")
            self.assertTrue(self.client.calls[-1][1]["readonly"])
            await self.broker.disconnect()
        self.assertEqual(self.client.whatifs, [])
        self.assertEqual(self.client.placed, [])

    async def test_direct_sdk_access_from_foreign_thread_is_refused(self):
        with self.assertRaises(IBKROperationError) as failure:
            await asyncio.to_thread(self.broker.health)
        self.assertEqual(failure.exception.code, "wrong_owner_thread")
        self.assertEqual(set(self.client.access_threads), {self.client.owner})


class ExecutionLifecycleTests(unittest.TestCase):
    def build(self):
        clients = []
        def factory():
            client = ExecutionClient()
            clients.append(client)
            return client
        config = paper_config()
        broker = IBKRAdapter(config, client_factory=factory, contract_factory=NS, order_factory=NS)
        lifecycle = IBKRBrokerLifecycle(broker, poll_interval=0.005)
        self.addCleanup(lifecycle.close)
        self.assertTrue(lifecycle.start())
        self.wait(lambda: lifecycle.paper_order_snapshot()["execution_enabled"])
        return lifecycle, clients[0], intent(config)

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("lifecycle did not reach expected state")

    def test_synchronous_facade_executes_all_sdk_calls_on_owner_loop(self):
        broker, client, request = self.build()
        ctx = context(broker)
        preview = broker.preview_paper_order(request, order_ref="facade-1", context=ctx)
        row = broker.submit_paper_order(request, order_ref="facade-1", preview=preview, context=ctx)
        self.assertEqual(row["status"], "working")
        result = broker.cancel_paper_order(order_ref="facade-1", broker_order_id=row["broker_order_id"], context=ctx)
        self.assertEqual(result["status"], "cancel_pending")
        self.assertNotEqual(client.owner, threading.get_ident())
        self.assertEqual(set(client.access_threads), {client.owner})
        copy = broker.orders_snapshot()
        copy[0]["status"] = "filled"
        self.assertEqual(broker.orders_snapshot()[0]["status"], "cancel_pending")
        self.assertTrue(broker.close())
        self.assertFalse(broker.paper_order_snapshot()["execution_enabled"])
        self.assertTrue(broker.paper_order_snapshot()["health_stale"])
        self.assertTrue(client.loop.is_closed())

    def test_close_cancels_inflight_submit_and_preserves_unknown(self):
        broker, client, request = self.build()
        ctx = context(broker)
        preview = broker.preview_paper_order(request, order_ref="closing-1", context=ctx)
        client.place_mode = "silent"
        results = []
        worker = threading.Thread(target=lambda: results.append(broker.submit_paper_order(
            request, order_ref="closing-1", preview=preview, context=ctx, timeout=30)))
        worker.start()
        self.wait(lambda: bool(client.placed))
        self.assertTrue(broker.close(timeout=2))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "unknown")
        self.assertEqual(broker.orders_snapshot()[0]["status"], "unknown")
        self.assertEqual(len(client.placed), 1)
        self.assertTrue(client.loop.is_closed())

    def test_domain_journal_accepts_real_facade_and_late_fill_cache(self):
        from yolo_trading.paper_order_store import PaperOrderStore
        from yolo_trading.paper_orders import PaperOrderService

        broker, client, request = self.build()
        caller = {"id": "mac-device", "platform": "macos", "permissions": ["devices:manage"]}
        with tempfile.TemporaryDirectory() as folder:
            service = PaperOrderService(PaperOrderStore(Path(folder) / "paper.sqlite3"), broker)
            status, body = service.operate("create", {"client_request_id": str(uuid4()), **request}, caller, "user")
            self.assertEqual(status, 201, body)
            row = body["order"]
            for action in ("preview", "confirm", "submit", "cancel"):
                data = {"client_request_id": str(uuid4()), "version": row["version"], "content_hash": row["content_hash"]}
                if action == "confirm":
                    data.update(preview_id=row["preview"]["id"], expires_at=row["preview"]["expires_at"],
                                user_action="confirm_paper_order")
                elif action == "submit":
                    data.update(confirmation_id=row["confirmation"]["id"], user_action="submit_paper_order")
                elif action == "cancel":
                    data.update(user_action="cancel_paper_order")
                status, body = service.operate(action, data, caller, "user", order_id=row["id"])
                self.assertEqual(status, 200, body)
                row = body["order"]
            self.assertEqual(row["state"], "cancel_pending")
            self.assertEqual(len(client.placed), 1)
            trade = client.placed[0]
            client.loop.call_soon_threadsafe(lambda: client.execDetailsEvent.emit(trade, client.fill(trade, "late-fill", "10")))
            self.wait(lambda: broker.orders_snapshot()[0]["status"] == "filled")
            result = service.get(row["id"], "user")
            self.assertEqual(result["order"]["state"], "filled")
            self.assertEqual(result["order"]["filled_quantity"], 10)
            self.assertNotIn("DU123456", json.dumps(result))
            self.assertEqual(result["events"][-1]["state"], "filled")

    def test_reconnect_reconciliation_persists_to_ios_read_model_without_resend(self):
        from yolo_trading.paper_order_store import PaperOrderStore
        from yolo_trading.paper_orders import PaperOrderService

        clients = []
        def factory():
            client = ExecutionClient()
            if clients:
                trade = deepcopy(clients[0].placed[0])
                trade.orderStatus = NS(status="Submitted", filled=0, avgFillPrice=0,
                                       permId=trade.order.permId)
                fill = client.fill(trade, "recovered-fill", "10", "101")
                fill.commissionReport = NS(execId="recovered-fill", commission=Decimal("1"), currency="USD")
                client.open_orders = [trade]
                client.execution_rows = [fill]
            clients.append(client)
            return client

        config = paper_config()
        broker = IBKRBrokerLifecycle(IBKRAdapter(config, client_factory=factory,
            contract_factory=NS, order_factory=NS), poll_interval=0.005, reconnect_interval=0.01)
        self.addCleanup(broker.close)
        self.assertTrue(broker.start())
        self.wait(lambda: broker.paper_order_snapshot()["execution_enabled"])
        caller = {"id": "mac-device", "platform": "macos", "permissions": ["devices:manage"]}
        with tempfile.TemporaryDirectory() as folder:
            service = PaperOrderService(PaperOrderStore(Path(folder) / "paper.sqlite3"), broker)
            _, body = service.operate("create", {"client_request_id": str(uuid4()), **intent(config)}, caller, "user")
            row = body["order"]
            for action in ("preview", "confirm", "submit"):
                data = {"client_request_id": str(uuid4()), "version": row["version"],
                        "content_hash": row["content_hash"]}
                if action == "confirm":
                    data.update(preview_id=row["preview"]["id"], expires_at=row["preview"]["expires_at"],
                                user_action="confirm_paper_order")
                elif action == "submit":
                    data.update(confirmation_id=row["confirmation"]["id"], user_action="submit_paper_order")
                status, body = service.operate(action, data, caller, "user", order_id=row["id"])
                self.assertEqual(status, 200, body)
                row = body["order"]
            self.assertEqual(row["state"], "working")

            clients[0].loop.call_soon_threadsafe(clients[0].disconnect)
            self.wait(lambda: len(clients) == 2 and broker.orders_snapshot()[0]["status"] == "filled")
            result = service.get(row["id"], "user")
            self.assertEqual((result["order"]["state"], result["order"]["filled_quantity"],
                              result["order"]["average_fill_price"]), ("filled", 10, "101"))
            self.assertEqual((len(clients[0].placed), len(clients[1].placed)), (1, 0))
            self.assertNotIn("DU123456", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
