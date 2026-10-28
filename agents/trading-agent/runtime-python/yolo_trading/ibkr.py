"""IBKR lifecycle, opt-in Paper limit orders and offline configuration inspection.

Import, construction and health() never open sockets. An integrating service
must explicitly await connect(). Execution is opt-in Paper only; no live orders.
"""

import argparse
import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import importlib
import importlib.util
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

from .ibkr_config import IBKRConfig, IBKRConfigurationError, load_ibkr_config


class IBKROperationError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    def as_dict(self):
        return {"code": self.code, "message": str(self)}


def _timeout(value):
    try:
        result = float(value)
        if isinstance(value, bool) or not 0 < result <= 60:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError):
        raise IBKROperationError("invalid_timeout", "超时必须大于 0 且不超过 60 秒。") from None


def _number(value):
    try:
        if isinstance(value, bool):
            return None
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) < Decimal("1e100") else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _intent(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value) is None:
        raise IBKROperationError("invalid_intent_id", "需要稳定的 1～64 位 ASCII intent_id。")
    return value


def _is_supported_paper_account(account):
    """Fail closed to Paper namespaces observed from IBKR-managed accounts.

    IBKR does not publish an account-number namespace contract.  Keep this an
    explicit allow-list instead of accepting every identifier beginning with
    ``DU``; the configured account must also be returned by managedAccounts().
    """
    return isinstance(account, str) and re.fullmatch(r"(?:DU|DUR)[0-9]+", account) is not None


def normalize_order(request):
    """Agent-owned order schema; rejects credentials, routing and short overrides."""
    allowed = {"symbol", "action", "quantity", "limit_price", "sec_type", "exchange",
               "currency", "tif", "order_type", "outside_rth"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise IBKROperationError("unsupported_order_field", "订单包含未知或不支持的字段。")
    required = {"symbol", "action", "quantity", "limit_price"}
    if not required <= request.keys():
        raise IBKROperationError("incomplete_order", "缺少股票、方向、数量或限价。")
    for key, expected in (("sec_type", "STK"), ("exchange", "SMART"), ("currency", "USD"),
                          ("tif", "DAY"), ("order_type", "LMT")):
        if request.get(key, expected) != expected:
            raise IBKROperationError("unsupported_order", "仅支持 STK/SMART/USD/DAY 限价单。")
    if request.get("outside_rth", False) is not False:
        raise IBKROperationError("outside_rth_not_supported", "不支持盘前盘后订单。")
    symbol = request["symbol"]
    if not isinstance(symbol, str) or re.fullmatch(r"[A-Z0-9][A-Z0-9 .-]{0,31}", symbol) is None:
        raise IBKROperationError("invalid_symbol", "股票代码必须是明确的 ASCII 股票标识。")
    if request["action"] not in ("BUY", "SELL"):
        raise IBKROperationError("unsupported_action", "仅支持 BUY 或 SELL，不支持 short 标记。")
    if type(request["quantity"]) is not int or request["quantity"] <= 0:
        raise IBKROperationError("invalid_quantity", "数量必须是正整数，不支持零股。")
    price = _number(request["limit_price"])
    if price is None or price <= 0:
        raise IBKROperationError("invalid_limit_price", "限价必须是有限正数。")
    return {"symbol": symbol, "action": request["action"], "quantity": request["quantity"],
            "limit_price": format(price.normalize(), "f"), "sec_type": "STK", "exchange": "SMART",
            "currency": "USD", "tif": "DAY", "order_type": "LMT", "outside_rth": False}


class IBKRAdapter:
    def __init__(self, config: IBKRConfig, *, client_factory=None, contract_factory=None, order_factory=None):
        if not isinstance(config, IBKRConfig):
            raise TypeError("config must be an IBKRConfig")
        self._config = config
        self._client_factory = client_factory
        self._client = None
        self._lock = asyncio.Lock()
        self._state = "disconnected"
        self._error = None
        self._attempted = False
        self._subscriptions = []
        self._revision = 1
        self._last_change = None
        self._contract_factory, self._order_factory = contract_factory, order_factory
        self._owner_loop, self._owner_thread = None, None
        self._account_verified = False
        self._orders, self._order_ids, self._perm_ids, self._exec_ids = {}, {}, {}, {}
        self._events = []
        self._session_id = None
        self._environment_verified = False
        self._reads = {name: False for name in ("account", "positions", "open_orders", "executions", "account_summary")}
        self._positions = []
        self._previews = {}
        self._connection_generation = 0
        self._reconciliation_generation = 0
        self._ready_generation = None
        self._reconciliation_phase = "idle"
        self._reconciliation_failure = None
        self._disconnecting_client = None

    def _assert_owner(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._owner_loop is not None and (loop is not self._owner_loop or threading.get_ident() != self._owner_thread):
            raise IBKROperationError("wrong_owner_thread", "SDK 操作必须在 Broker 生命周期所属 asyncio 线程执行。")

    def _bind_owner(self):
        self._assert_owner()
        if self._owner_loop is None:
            self._owner_loop, self._owner_thread = asyncio.get_running_loop(), threading.get_ident()

    @property
    def config(self):
        return self._config

    def _dependency_available(self):
        if self._client_factory is not None:
            return True
        try:
            return importlib.util.find_spec("ib_async") is not None
        except (ImportError, ValueError):
            return False

    def health(self):
        if self._client is not None:
            self._assert_owner()
        available = self._dependency_available()
        blockers = []
        if not available:
            blockers.append({"code": "dependency_missing", "message": "未安装可选依赖 ib_async。"})
        if self._config.account is None:
            blockers.append({"code": "account_required", "message": "连接前需显式配置目标账户 account。"})
        if self._account_verified and self._reconciliation_phase != "ready":
            blockers.append({"code": "reconciliation_incomplete", "message": "必需的启动读取未全部完成，禁止订单操作。"})
        if self._config.paper_execution_enabled and self._account_verified and not self._environment_verified:
            blockers.append({"code": "paper_environment_unverified", "message": "未核验受支持的 Paper 账户，禁止执行。"})
        state = self._state
        if state == "disconnected" and blockers:
            state = "dependency_missing" if not available else "configuration_incomplete"
        if self._client is not None and not self._client.isConnected() and state not in ("connecting", "disconnected"):
            state = "gateway_unavailable"
        connected = bool(self._client is not None and self._client.isConnected())
        ready = bool(connected and self._account_verified and self._reconciliation_phase == "ready"
                     and self._ready_generation == self._reconciliation_generation and all(self._reads.values()))
        return {"state": state, "configuration": self._config.audit(),
                "configuration_revision": self._revision, "last_configuration_change": self._last_change,
                "dependency_available": available, "test_client": self._client_factory is not None,
                "connection_attempted": self._attempted,
                "connected": connected, "ready": ready,
                "environment_verified": self._environment_verified, "data_complete": ready,
                "session_id": self._session_id, "account_verified": self._account_verified,
                "permissions_verified": self._config.paper_execution_enabled and self._account_verified,
                "reconciliation": dict(self._reads),
                "reconciliation_status": {"phase": self._reconciliation_phase,
                                          "connection_generation": self._connection_generation,
                                          "generation": self._reconciliation_generation,
                                          "ready_generation": self._ready_generation,
                                          "failure": deepcopy(self._reconciliation_failure)},
                "execution_enabled": bool(self._config.paper_execution_enabled and ready
                                          and self._environment_verified
                                          and self._state == "connected_paper"),
                "tracked_orders": len(self._orders), "blockers": blockers, "error": self._error}

    def _fail(self, state, code, message):
        self._state, self._error = state, {"code": code, "message": message}

    def _invalidate_reconciliation(self, phase, *, code=None, message=None):
        """Retire every in-flight read before publishing a non-ready state."""
        self._reconciliation_generation += 1
        self._ready_generation = None
        self._reconciliation_phase = phase
        self._reconciliation_failure = ({"code": code, "message": message} if code is not None else None)
        self._account_verified = False
        self._environment_verified = False
        self._reads = dict.fromkeys(self._reads, False)
        self._positions = []
        self._session_id = None
        self._previews.clear()

    def _begin_reconciliation(self):
        self._invalidate_reconciliation("verifying_account")
        return self._reconciliation_generation

    def _current_attempt(self, client, connection_generation, reconciliation_generation=None):
        current = self._client is client and self._connection_generation == connection_generation
        if reconciliation_generation is not None:
            current = current and self._reconciliation_generation == reconciliation_generation
        return current

    def _set_reconciliation_failure(self, code, message, *, step=None):
        self._ready_generation = None
        self._reconciliation_phase = "failed"
        self._reconciliation_failure = {"code": code, "message": message}
        if step is not None:
            self._reconciliation_failure["step"] = step
        self._session_id = None
        self._previews.clear()
        self._fail("reconciliation_failed", code, message)

    def _bind_event(self, client, connection_generation, name, callback):
        event = getattr(client, name, None)
        if event is None:
            if self._config.paper_execution_enabled:
                raise RuntimeError("Required execution events unavailable")
            return

        def current_session_handler(*args):
            if (self._disconnecting_client is client
                    or not self._current_attempt(client, connection_generation)):
                return
            callback(*args)

        event.connect(current_session_handler)
        self._subscriptions.append((event, current_session_handler))

    def _disconnect_client(self):
        self._assert_owner()
        client = self._client
        # Retire the active session before touching the SDK.  A disconnect can
        # raise while the socket still looks connected; that uncertainty must
        # never preserve a previously ready/executable generation.
        self._invalidate_reconciliation("disconnecting")
        if client is not None:
            self._disconnecting_client = client
            try:
                client.disconnect()
                if client.isConnected():
                    raise ConnectionError
            except Exception:
                self._disconnecting_client = None
                self._reconciliation_phase = "disconnect_failed"
                self._reconciliation_failure = {
                    "code": "disconnect_failed",
                    "message": "无法确认原 Gateway 连接已关闭。",
                }
                self._fail("disconnect_failed", "disconnect_failed", "无法确认原 Gateway 连接已关闭，配置切换已阻止。")
                return False
        for event, handler in self._subscriptions:
            event.disconnect(handler)
        self._subscriptions.clear()
        self._disconnecting_client = None
        self._client = None
        self._connection_generation += 1
        self._reconciliation_phase = "idle"
        self._reconciliation_failure = None
        return True

    def _on_disconnected(self, *args):
        self._assert_owner()
        self._invalidate_reconciliation("connection_lost", code="connection_lost",
                                        message="Gateway 连接已断开，需重新连接并完整对账。")
        self._mark_orders_unknown("connection_lost")
        self._fail("gateway_unavailable", "connection_lost", "Gateway 连接已断开，需恢复后重新对账。")

    def _on_error(self, request_id, error_code, error_text, *args):
        self._assert_owner()
        # Never publish/log broker error_text: it can contain account details.
        if error_code in (1100, 1300, 2110):
            self._invalidate_reconciliation("connection_lost", code="broker_connection_lost",
                                            message="Gateway 与 IBKR 的连接异常，等待恢复后重新对账。")
            self._mark_orders_unknown("broker_connection_lost")
            self._fail("broker_unavailable", "broker_connection_lost", "Gateway 与 IBKR 的连接异常，当前数据不可视为最新。")
        elif error_code in (1101, 1102):
            self._invalidate_reconciliation("reconciliation_required", code="connection_restored",
                                            message="券商连接已恢复，必须完整重跑启动对账。")
            self._fail("reconciliation_required", "reconciliation_required", "券商连接恢复，需重新核对账户、挂单、成交和订阅。")
        elif error_code == 10197:
            self._invalidate_reconciliation("session_conflict", code="session_conflict",
                                            message="检测到竞争登录会话，当前数据失效。")
            self._mark_orders_unknown("session_conflict")
            self._fail("session_conflict", "session_conflict", "存在竞争登录会话，请检查 Gateway 登录状态。")
        record = self._orders.get(self._order_ids.get((self._config.client_id, request_id)))
        if record is not None and type(error_code) is int and error_code < 2000:
            record["error_code"] = error_code
            if error_code == 201:
                self._status(record, "rejected")
            elif error_code == 202:
                self._status(record, "cancelled")
            else:
                self._unknown(record, "broker_error")
            record["_ack"].set()
            self._record_event(record, "error")

    async def connect(self):
        self._bind_owner()
        async with self._lock:
            if self._client is not None:
                # An existing unhealthy connection requires explicit disconnect
                # and re-reconciliation, never a silent configuration fallback.
                return self.health()
            if self._config.account is None or not self._dependency_available():
                return self.health()
            self._state, self._error, self._attempted = "connecting", None, True
            self._invalidate_reconciliation("connecting")
            try:
                factory = self._client_factory
                if factory is None:
                    try:
                        factory = importlib.import_module("ib_async").IB
                    except (ImportError, AttributeError):
                        self._fail("dependency_unavailable", "dependency_unavailable", "ib_async 无法加载，请核对可选依赖及版本。")
                        return self.health()
                client = factory()
                self._client = client
                self._connection_generation += 1
                connection_generation = self._connection_generation
                client.RaiseRequestErrors = True
                for name, handler in (("disconnectedEvent", self._on_disconnected), ("errorEvent", self._on_error),
                                      ("openOrderEvent", self._on_open_order), ("orderStatusEvent", self._on_order_status),
                                      ("execDetailsEvent", self._on_execution), ("commissionReportEvent", self._on_commission)):
                    self._bind_event(client, connection_generation, name, handler)
                await asyncio.wait_for(client.connectAsync(
                    self._config.host, self._config.port, clientId=self._config.client_id,
                    timeout=self._config.connect_timeout, readonly=not self._config.paper_execution_enabled,
                    account=self._config.account, raiseSyncErrors=True,
                ), timeout=self._config.connect_timeout)
                if not client.isConnected():
                    raise ConnectionError
                await self._verify_and_reconcile(client, connection_generation)
            except asyncio.CancelledError:
                if self._disconnect_client():
                    self._fail("disconnected", "connection_cancelled", "连接已取消。")
                raise
            except Exception:
                if self._disconnect_client():
                    self._reconciliation_phase = "failed"
                    self._reconciliation_failure = {"code": "connection_failed",
                        "message": "无法完成 Gateway 连接或启动握手。"}
                    self._fail("gateway_unavailable", "connection_failed", "无法完成 Gateway 连接；请检查进程、登录/API 配置、端口及 client ID。")
            return self.health()

    async def _verify_and_reconcile(self, client, connection_generation):
        reconciliation_generation = self._begin_reconciliation()
        try:
            accounts = client.managedAccounts()
        except Exception:
            accounts = None
        if not self._current_attempt(client, connection_generation, reconciliation_generation):
            return False
        if (not isinstance(accounts, (list, tuple))
                or sum(account == self._config.account for account in accounts) != 1):
            if self._disconnect_client():
                self._set_reconciliation_failure("target_account_not_unique",
                    "Gateway 未唯一返回配置的目标账户；已断开且不会自动选择其他账户。", step="account")
                self._fail("account_mismatch", "target_account_not_unique",
                           "Gateway 未唯一返回配置目标账户；已断开，未自动选择其他账户。")
            return False
        self._account_verified = True
        self._environment_verified = (self._config.environment == "paper"
            and _is_supported_paper_account(self._config.account))
        self._reads["account"] = True
        self._reconciliation_phase = "reconciling"
        results = await self._reconcile(client, connection_generation, reconciliation_generation)
        if not self._current_attempt(client, connection_generation, reconciliation_generation):
            return False
        self._reads.update({name: success for name, success, _, _ in results})
        first_failure = next((item for item in results if not item[1]), None)
        if first_failure is not None:
            name, _, _, reason = first_failure
            self._set_reconciliation_failure("reconciliation_read_failed",
                "启动对账的必需读取未完成。", step=name)
            self._reconciliation_failure["reason"] = reason
            return False
        if not client.isConnected():
            self._invalidate_reconciliation("connection_lost", code="connection_lost",
                                            message="Gateway 在启动对账完成前断开。")
            self._fail("gateway_unavailable", "connection_lost", "Gateway 在启动对账完成前断开。")
            return False
        positions = next(value for name, _, value, _ in results if name == "positions")
        self._positions = [row for row in positions if getattr(row, "account", None) == self._config.account]
        self._ready_generation = reconciliation_generation
        self._reconciliation_phase = "ready"
        self._reconciliation_failure = None
        self._session_id = str(uuid4())
        self._state = "connected_paper" if self._config.paper_execution_enabled else "connected_read_only"
        self._error = None
        return True

    async def _reconcile(self, client, connection_generation, reconciliation_generation):
        methods = (("positions", "reqPositionsAsync", ()),
                   ("open_orders", "reqAllOpenOrdersAsync", ()),
                   ("executions", "reqExecutionsAsync", ()),
                   ("account_summary", "accountSummaryAsync", (self._config.account,)))
        async def read(name, method, args):
            call = getattr(client, method, None)
            if call is None:
                return name, False, None, "method_unavailable"
            try:
                rows = await asyncio.wait_for(call(*args), self._config.connect_timeout)
                if not isinstance(rows, (list, tuple)):
                    return name, False, None, "invalid_response"
                if name == "account_summary" and not any(getattr(row, "account", None) == self._config.account for row in rows):
                    return name, False, None, "target_account_missing"
                return name, True, rows, None
            except asyncio.TimeoutError:
                return name, False, None, "timeout"
            except Exception:
                return name, False, None, "request_failed"
        results = await asyncio.gather(*(read(*item) for item in methods))
        if not self._current_attempt(client, connection_generation, reconciliation_generation):
            return tuple(results)
        return tuple(results)

    async def reconcile(self):
        """Revalidate the exact account and every required read on the current connection."""
        self._assert_owner()
        async with self._lock:
            client = self._client
            connection_generation = self._connection_generation
            if client is None or not client.isConnected():
                self._invalidate_reconciliation("connection_lost", code="connection_lost",
                                                message="Gateway 未连接，无法执行启动对账。")
                self._fail("gateway_unavailable", "connection_lost", "Gateway 未连接，无法执行启动对账。")
                return self.health()
            self._state, self._error = "reconciling", None
            await self._verify_and_reconcile(client, connection_generation)
            return self.health()

    async def disconnect(self):
        self._assert_owner()
        async with self._lock:
            if self._disconnect_client():
                self._state, self._error = "disconnected", None
            return self.health()

    async def reconfigure(self, config: IBKRConfig):
        self._assert_owner()
        if not isinstance(config, IBKRConfig):
            raise TypeError("config must be an IBKRConfig")
        async with self._lock:
            previous = self._config.config_id
            if not self._disconnect_client():
                return self.health()
            self._config = config
            self._revision += 1
            self._state, self._error, self._attempted = "disconnected", None, False
            self._last_change = {"from_config_id": previous, "to_config_id": config.config_id,
                                 "changed_at": datetime.now(timezone.utc).isoformat()}
            # Switching is offline and never reconnects or retries an old action.
            return self.health()

    def paper_order_snapshot(self):
        health = self.health()
        return {"environment": self.config.environment, "config_id": self.config.config_id,
                "session_id": self._session_id, "read_only": self.config.read_only,
                **{name: health[name] for name in ("account_verified", "permissions_verified", "environment_verified",
                   "execution_enabled", "connected", "data_complete")}, "health_stale": False}

    def _check_context(self, context):
        self._assert_owner()
        if not isinstance(context, dict) or context != {"config_id": self.config.config_id, "session_id": self._session_id}:
            raise IBKROperationError("stale_broker_context", "配置或券商会话已变化，需要重新预览与确认。")

    def _execution_guard(self, context):
        self._check_context(context)
        if not self.config.paper_execution_enabled:
            raise IBKROperationError("execution_disabled", "仅显式开启的 Paper 执行配置可操作订单。")
        ready = self.paper_order_snapshot()
        if not all(ready[key] for key in ("account_verified", "permissions_verified", "environment_verified",
                                         "execution_enabled", "connected", "data_complete")):
            raise IBKROperationError("broker_not_ready", "账户、环境、连接或必需对账尚未满足要求。")
        if self.config.account not in self._client.managedAccounts():
            self._account_verified = False
            raise IBKROperationError("account_mismatch", "当前连接不再包含目标账户。")

    def _request(self, intent):
        allowed = {"account_selection", "contract", "side", "quantity", "limit_price", "tif", "order_type"}
        if not isinstance(intent, dict) or set(intent) != allowed:
            raise IBKROperationError("unsupported_order_field", "订单意图包含未知或缺失字段。")
        contract = intent["contract"]
        if not isinstance(contract, dict) or set(contract) != {"symbol", "sec_type", "exchange", "currency"}:
            raise IBKROperationError("unsupported_contract", "只接受明确的股票合约字段。")
        if intent["account_selection"] != self.config.config_id:
            raise IBKROperationError("account_selection_mismatch", "账户选择与当前配置不一致。")
        return normalize_order({**contract, "action": intent["side"], "quantity": intent["quantity"],
                                "limit_price": intent["limit_price"], "tif": intent["tif"], "order_type": intent["order_type"]})

    def _types(self):
        if self._contract_factory is None or self._order_factory is None:
            sdk = importlib.import_module("ib_async")
            return self._contract_factory or sdk.Contract, self._order_factory or sdk.Order
        return self._contract_factory, self._order_factory

    async def _qualify(self, request, timeout):
        contract_type, _ = self._types()
        query = contract_type(symbol=request["symbol"], secType="STK", exchange="SMART", currency="USD")
        try:
            details = await asyncio.wait_for(self._client.reqContractDetailsAsync(query), timeout)
        except Exception:
            raise IBKROperationError("contract_lookup_failed", "合约确认失败或超时。") from None
        if not isinstance(details, (list, tuple)) or len(details) != 1:
            raise IBKROperationError("contract_not_unique", "必须恰好匹配一个 ContractDetails。")
        contract = deepcopy(getattr(details[0], "contract", None))
        if (contract is None or type(getattr(contract, "conId", None)) is not int or contract.conId <= 0
                or getattr(contract, "symbol", None) != request["symbol"] or getattr(contract, "secType", None) != "STK"
                or getattr(contract, "currency", None) != "USD" or getattr(contract, "exchange", None) != "SMART"):
            raise IBKROperationError("invalid_qualified_contract", "返回合约的唯一标识或股票/路由/币种不一致。")
        return contract

    @staticmethod
    def _contract_snapshot(contract):
        return {"con_id": contract.conId, "symbol": contract.symbol, "sec_type": contract.secType,
                "exchange": contract.exchange, "currency": contract.currency}

    def _order(self, request, order_ref, *, what_if=False, order_id=0):
        _, order_type = self._types()
        return order_type(orderId=order_id, clientId=self.config.client_id, account=self.config.account,
                          action=request["action"], totalQuantity=request["quantity"], orderType="LMT",
                          lmtPrice=Decimal(request["limit_price"]), tif="DAY", outsideRth=False,
                          orderRef=order_ref, whatIf=what_if, transmit=True, shortSaleSlot=0)

    async def preview_paper_order(self, intent, *, order_ref, context, timeout=5.0):
        self._assert_owner()
        reference, request, timeout = _intent(order_ref), self._request(intent), _timeout(timeout)
        async with self._lock:
            self._execution_guard(context)
            self._previews.pop(reference, None)
            contract = await self._qualify(request, timeout)
            self._execution_guard(context)
            try:
                state = await asyncio.wait_for(self._client.whatIfOrderAsync(
                    contract, self._order(request, reference, what_if=True)), timeout)
            except Exception:
                raise IBKROperationError("what_if_failed", "What-if 被拒绝或无法完成；未提交订单。") from None
            self._execution_guard(context)
            numbers = {name: _number(getattr(state, sdk_name, None)) for name, sdk_name in (
                ("initial_margin_change", "initMarginChange"), ("maintenance_margin_change", "maintMarginChange"),
                ("commission", "commission"))}
            currency = getattr(state, "commissionCurrency", None)
            complete = all(value is not None for value in numbers.values()) and currency == "USD"
            accepted = (complete and numbers["commission"] >= 0 and not getattr(state, "warningText", "")
                        and getattr(state, "status", "") not in ("Inactive", "Cancelled", "ApiCancelled"))
            result = {"qualified_contract": self._contract_snapshot(contract), "context": dict(context),
                      "what_if": {"complete": complete, "accepted": bool(accepted), "currency": currency if currency == "USD" else None,
                                  **{key: format(value, "f") if value is not None else None for key, value in numbers.items()}}}
            if not accepted:
                raise IBKROperationError("what_if_incomplete", "What-if 不完整、有警告或未获接受，禁止提交。")
            self._previews[reference] = {"request": request, "contract": contract, "result": deepcopy(result),
                                         "expires": asyncio.get_running_loop().time() + 120}
            return deepcopy(result)

    async def submit_paper_order(self, intent, *, order_ref, preview, context, timeout=5.0):
        self._assert_owner()
        reference, request, timeout = _intent(order_ref), self._request(intent), _timeout(timeout)
        async with self._lock:
            self._check_context(context)
            if reference in self._orders:
                record = self._orders[reference]
                if record["request"] != request or record["config_id"] != self.config.config_id:
                    raise IBKROperationError("intent_conflict", "同一 intent 标识不能用于不同订单或配置。")
                return self._public_order(record)
            self._execution_guard(context)
            saved = self._previews.get(reference)
            if (saved is None or saved["request"] != request or saved["expires"] < asyncio.get_running_loop().time()
                    or not isinstance(preview, dict) or preview.get("qualified_contract") != saved["result"]["qualified_contract"]
                    or preview.get("what_if") != saved["result"]["what_if"] or preview.get("context") != context
                    or saved["result"]["context"] != context):
                raise IBKROperationError("preview_required", "需要当前配置、会话和订单内容对应的有效 What-if 预览。")
            if any(row["status"] == "unknown" for row in self._orders.values()):
                raise IBKROperationError("unresolved_order", "已有结果未知的订单，先对账再提交新订单。")
            try:
                order_id = self._client.client.getReqId()
            except Exception:
                raise IBKROperationError("order_id_unavailable", "连接未提供可用的 nextValidId。") from None
            if type(order_id) is not int or order_id <= 0 or (self.config.client_id, order_id) in self._order_ids:
                raise IBKROperationError("invalid_order_id", "无法安全分配新的券商订单 ID。")
            order = self._order(request, reference, order_id=order_id)
            record = {"order_ref": reference, "broker_order_id": str(order_id), "order_id": order_id,
                      "client_id": self.config.client_id, "perm_id": None, "config_id": self.config.config_id,
                      "session_id": self._session_id, "request": request, "qualified_contract": saved["result"]["qualified_contract"],
                      "status": "unknown", "filled_quantity": 0, "average_fill_price": None, "error_code": None,
                      "unknown_reason": "pending_submission", "executions": {}, "_account": self.config.account,
                      "_order": order, "_ack": asyncio.Event(), "_cancel_attempted": False,
                      "_integrity_failed": False, "_last": None}
            # Reserve the intent and ID before the only call that may transmit.
            self._orders[reference] = record
            self._order_ids[(self.config.client_id, order_id)] = reference
            self._record_event(record, "submitting")
            try:
                trade = self._client.placeOrder(saved["contract"], order)
                if trade is None:
                    self._unknown(record, "missing_submit_result")
                else:
                    self._observe_trade(trade, "submit_return")
                    if not record["_ack"].is_set():
                        await asyncio.wait_for(record["_ack"].wait(), timeout)
            except asyncio.CancelledError:
                self._unknown(record, "submission_cancelled_after_send")
                self._record_event(record, "unknown")
                raise
            except Exception:
                self._unknown(record, "submit_outcome_unknown")
            self._record_event(record, "submit_result")
            return self._public_order(record)

    async def cancel_paper_order(self, *, order_ref, broker_order_id, context, timeout=5.0):
        self._assert_owner()
        reference = _intent(order_ref)
        _timeout(timeout)
        async with self._lock:
            self._execution_guard(context)
            record = self._orders.get(reference)
            if (record is None or record["broker_order_id"] != broker_order_id
                    or record["client_id"] != self.config.client_id or record["_account"] != self.config.account
                    or record["config_id"] != self.config.config_id or record["session_id"] != self._session_id):
                raise IBKROperationError("order_not_owned", "只允许取消本进程登记、同账户同 client ID 的订单。")
            if record["_cancel_attempted"] or record["status"] in ("filled", "cancelled", "rejected"):
                return self._public_order(record)
            record["_cancel_attempted"] = True
            self._status(record, "cancel_pending")
            try:
                trade = self._client.cancelOrder(record["_order"])
                if trade is None:
                    self._unknown(record, "cancel_outcome_unknown")
                else:
                    self._observe_trade(trade, "cancel_return")
            except asyncio.CancelledError:
                self._unknown(record, "cancel_outcome_unknown")
                self._record_event(record, "cancel_unknown")
                raise
            except Exception:
                self._unknown(record, "cancel_outcome_unknown")
            self._record_event(record, "cancel_result")
            return self._public_order(record)

    def _public_order(self, record):
        return deepcopy({key: value for key, value in record.items() if not key.startswith("_")})

    def orders_snapshot(self):
        self._assert_owner()
        return [self._public_order(record) for record in self._orders.values()]

    def events_snapshot(self, after=0):
        self._assert_owner()
        if type(after) is not int or after < 0:
            raise IBKROperationError("invalid_cursor", "事件游标必须为非负整数。")
        return deepcopy([event for event in self._events if event["seq"] > after])

    def _record_event(self, record, kind):
        value = self._public_order(record)
        if value != record["_last"]:
            self._events.append({"seq": len(self._events) + 1, "kind": kind, "order": value})
            record["_last"] = deepcopy(value)

    def _status(self, record, status):
        if record["_integrity_failed"]:
            return
        if record["filled_quantity"] >= record["request"]["quantity"]:
            status = "filled"
        elif record["status"] in ("filled", "cancelled", "rejected"):
            return
        elif record["status"] == "cancel_pending" and status in ("accepted", "working", "partial"):
            return
        elif status in ("accepted", "working") and record["filled_quantity"] > 0:
            status = "partial"
        elif record["status"] == "working" and status == "accepted":
            return
        if status == "rejected" and record["filled_quantity"]:
            status = "unknown"
        record["status"] = status
        if status != "unknown":
            record["unknown_reason"] = None

    def _unknown(self, record, reason, *, integrity=False):
        if integrity:
            record["_integrity_failed"] = True
        if integrity or record["status"] not in ("filled", "cancelled", "rejected"):
            record["status"], record["unknown_reason"] = "unknown", reason
        record["_ack"].set()

    def _mark_orders_unknown(self, reason):
        for record in self._orders.values():
            self._unknown(record, reason)
            self._record_event(record, "disconnected")

    def _bind_perm_id(self, record, perm_id):
        if type(perm_id) is int and perm_id > 0:
            if (self._perm_ids.get(perm_id, record["order_ref"]) != record["order_ref"]
                    or record["perm_id"] not in (None, perm_id)):
                self._unknown(record, "identity_conflict", integrity=True)
                self._record_event(record, "identity_conflict")
                return False
            record["perm_id"] = perm_id
            self._perm_ids[perm_id] = record["order_ref"]
        return True

    def _owned_trade(self, trade):
        order = getattr(trade, "order", None)
        record = self._orders.get(self._order_ids.get((getattr(order, "clientId", None), getattr(order, "orderId", None))))
        if (record is None or getattr(order, "account", None) != record["_account"]
                or getattr(order, "orderRef", None) != record["order_ref"]
                or getattr(getattr(trade, "contract", None), "conId", None) != record["qualified_contract"]["con_id"]):
            return None
        for source in (order, getattr(trade, "orderStatus", None)):
            if not self._bind_perm_id(record, getattr(source, "permId", 0)):
                return None
        return record

    def _observe_trade(self, trade, kind):
        self._assert_owner()
        record = self._owned_trade(trade)
        if record is None:
            return
        status = getattr(trade, "orderStatus", None)
        amount = _number(getattr(status, "filled", 0))
        if amount is not None and amount >= 0 and amount == amount.to_integral_value():
            if amount > record["request"]["quantity"]:
                self._unknown(record, "inconsistent_fill", integrity=True)
                self._record_event(record, "inconsistent_fill")
                return
            record["filled_quantity"] = max(record["filled_quantity"], int(amount))
        average = _number(getattr(status, "avgFillPrice", None))
        if amount is not None and amount >= record["filled_quantity"] and average is not None and average > 0:
            record["average_fill_price"] = format(average, "f")
        raw = getattr(status, "status", "")
        mapping = {"Submitted": "working", "PreSubmitted": "accepted", "PendingCancel": "cancel_pending",
                   "Cancelled": "cancelled", "ApiCancelled": "cancelled"}
        if raw in mapping:
            self._status(record, mapping[raw])
            record["_ack"].set()
        elif raw == "Filled":
            if record["filled_quantity"] == record["request"]["quantity"]:
                self._status(record, "filled")
            else:
                self._unknown(record, "incomplete_fill_details")
            record["_ack"].set()
        elif kind == "open_order" and raw in ("PendingSubmit", "ApiPending"):
            self._status(record, "accepted")
            record["_ack"].set()
        elif raw == "Inactive":
            self._unknown(record, "inactive_order")
        self._record_event(record, kind)

    def _on_open_order(self, trade):
        self._observe_trade(trade, "open_order")

    def _on_order_status(self, trade):
        self._observe_trade(trade, "order_status")

    def _on_execution(self, trade, fill):
        self._assert_owner()
        record = self._owned_trade(trade)
        execution = getattr(fill, "execution", None)
        if (record is None or getattr(execution, "acctNumber", None) != record["_account"]
                or getattr(execution, "clientId", None) != record["client_id"]
                or getattr(execution, "orderId", None) != record["order_id"]
                or getattr(getattr(fill, "contract", None), "conId", None) != record["qualified_contract"]["con_id"]):
            return
        if not self._bind_perm_id(record, getattr(execution, "permId", 0)):
            return
        exec_id = getattr(execution, "execId", None)
        shares, price = _number(getattr(execution, "shares", None)), _number(getattr(execution, "price", None))
        if (not isinstance(exec_id, str) or not 1 <= len(exec_id) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in exec_id)
                or shares is None or shares <= 0 or shares != shares.to_integral_value() or price is None or price <= 0):
            self._unknown(record, "invalid_execution", integrity=True)
            self._record_event(record, "invalid_execution")
            return
        if self._exec_ids.get(exec_id, record["order_ref"]) != record["order_ref"]:
            self._unknown(record, "execution_identity_conflict", integrity=True)
            self._record_event(record, "execution_identity_conflict")
            return
        item = {"exec_id": exec_id, "quantity": int(shares), "price": format(price, "f"), "commission": None}
        previous = record["executions"].get(exec_id)
        if previous is not None:
            if any(previous[key] != item[key] for key in ("quantity", "price")):
                self._unknown(record, "execution_conflict", integrity=True)
                self._record_event(record, "execution_conflict")
            return
        self._exec_ids[exec_id] = record["order_ref"]
        record["executions"][exec_id] = item
        total = sum(row["quantity"] for row in record["executions"].values())
        if total > record["request"]["quantity"]:
            self._unknown(record, "inconsistent_fill", integrity=True)
        else:
            record["filled_quantity"] = max(record["filled_quantity"], total)
            if total == record["filled_quantity"]:
                value = sum(Decimal(row["price"]) * row["quantity"] for row in record["executions"].values()) / total
                record["average_fill_price"] = format(value, "f")
            self._status(record, "partial")
        record["_ack"].set()
        self._record_event(record, "execution")

    def _on_commission(self, trade, fill, report):
        self._on_execution(trade, fill)
        record = self._owned_trade(trade)
        exec_id = getattr(report, "execId", None)
        execution = getattr(fill, "execution", None)
        if (record is None or exec_id not in record["executions"]
                or getattr(execution, "execId", None) != exec_id
                or getattr(execution, "acctNumber", None) != record["_account"]
                or getattr(execution, "clientId", None) != record["client_id"]
                or getattr(execution, "orderId", None) != record["order_id"]
                or getattr(getattr(fill, "contract", None), "conId", None) != record["qualified_contract"]["con_id"]):
            return
        commission = _number(getattr(report, "commission", None))
        currency = getattr(report, "currency", None)
        if commission is not None and currency == "USD":
            record["executions"][exec_id]["commission"] = format(commission, "f")
            self._record_event(record, "commission")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect IBKR configuration without connecting to Gateway.")
    parser.add_argument("--config", type=Path, help="JSON config; no username/password fields")
    parser.add_argument("--environment", choices=("paper", "live"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--client-id", type=int)
    parser.add_argument("--account")
    parser.add_argument("--read-only", choices=("true", "false"))
    parser.add_argument("--execution-enabled", choices=("true", "false"))
    parser.add_argument("--connect-timeout", type=float)
    args = parser.parse_args(argv)
    try:
        base = json.loads(args.config.read_text()) if args.config else None
        overrides = {key: value for key, value in vars(args).items() if key != "config" and value is not None}
        config = load_ibkr_config(base, overrides=overrides)
        health = IBKRAdapter(config).health()
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(json.dumps({"state": "configuration_error", "error": {"code": "config_file_unreadable", "message": "无法读取配置 JSON 文件。"}}, ensure_ascii=False))
        return 2
    except IBKRConfigurationError as exc:
        print(json.dumps({"state": "configuration_error", "error": exc.as_dict()}, ensure_ascii=False))
        return 2
    print(json.dumps(health, ensure_ascii=False))
    return 1 if health["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
