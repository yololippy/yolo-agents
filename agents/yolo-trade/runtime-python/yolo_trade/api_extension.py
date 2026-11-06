"""YoloTrade-only additions to the shared Yolo Agents chat API."""

import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time
from uuid import UUID, uuid5
from urllib.parse import parse_qs, urlsplit

from yolo_agents.api import APIError, identifier, text_field
from yolo_agents.agent_runtime import (
    RuntimeConfigurationError, RuntimeExecutionError, RuntimeProviderTimeoutError,
)
from .runtime_tools import (
    MARKET_QUOTE_TOOL, OPEN_ORDER_TOOL, OPEN_ORDERS_TOOL, PAPER_ORDER_TOOL,
    PAPER_ORDERS_TOOL, POSITIONS_TOOL, RuntimeToolContext,
)

from .paper_order_store import PaperOrderStore
from .paper_order_broker import BrokerBoundaryError, snapshot
from .paper_orders import OrderError, PaperOrderService
from .risk_policy import DEFAULT_RISK_POLICY, RiskPolicy
from .trading_reads import public_read


def read_order_json(handler):
    """Orders reject duplicate keys, non-finite numbers and ambiguous framing."""
    lengths = handler.headers.get_all("Content-Length", [])
    if handler.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
        raise APIError(400, "invalid_body", "订单请求体长度或编码无效。")
    if len(lengths[0]) > 10:
        raise APIError(413, "body_too_large", "订单请求体超过限制。")
    size = int(lengths[0])
    if not 0 < size <= 65536:
        raise APIError(413, "body_too_large", "订单请求体必须在 1 至 65536 字节之间。")
    if handler.headers.get_content_type() != "application/json" or handler.headers.get_content_charset("utf-8") != "utf-8":
        raise APIError(415, "json_required", "订单请求必须使用 UTF-8 application/json。")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError()

    try:
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise ValueError()
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
        # Also reject escaped lone surrogates before hashing or persisting text.
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise APIError(400, "invalid_json", "订单 JSON 无效，不允许重复字段或非有限数值。") from None


class TradingAPIExtension:
    """Keeps IBKR status and the execution boundary outside shared packages."""

    def __init__(self, broker=None, *, orders_path=None,
                 risk_policy: RiskPolicy | None = DEFAULT_RISK_POLICY):
        self.broker = broker
        self.risk_policy = risk_policy
        # A migration failure propagates to Host startup; never enables a fallback.
        self._order_store = PaperOrderStore(orders_path) if orders_path is not None else None
        self._orders = None
        self._order_guard = threading.RLock()
        self._chat_service = None
        self._auth = None
        self._tool_bound = False
        self._full_test_grants = {}
        self._full_test_grant_guard = threading.Lock()

    @staticmethod
    def _grant_digest(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _issue_simulator_probe_session(self, auth):
        pairing_code = auth.issue_pairing_code("ios", ttl=300)
        grant = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + 300
        with self._full_test_grant_guard:
            current = time.monotonic()
            self._full_test_grants = {
                key: expiry for key, expiry in self._full_test_grants.items()
                if expiry > current
            }
            self._full_test_grants[self._grant_digest(grant)] = expires_at
        return {"pairing_code": pairing_code, "grant": grant, "expires_in": 300}

    def _consume_simulator_probe_grant(self, value):
        if not isinstance(value, str) or not 16 <= len(value) <= 1024:
            raise APIError(400, "invalid_input", "Simulator 探针授权格式无效。")
        with self._full_test_grant_guard:
            expiry = self._full_test_grants.pop(self._grant_digest(value), None)
        if expiry is None or expiry <= time.monotonic():
            raise APIError(403, "invalid_probe_grant", "Simulator 探针授权无效、已过期或已使用。")

    def _run_full_test_model_probe(self, service):
        probe = getattr(service.runtime, "acceptance_probe", None)
        if not callable(probe):
            raise APIError(409, "probe_not_available", "当前 Provider 模式不支持模型验收探针。")
        try:
            result = probe()
        except RuntimeProviderTimeoutError:
            raise APIError(504, "probe_timeout", "模型验收探针超时，已取消本次请求。") from None
        except (RuntimeConfigurationError, RuntimeExecutionError, RuntimeError):
            raise APIError(503, "probe_failed", "模型验收探针未能完成，请检查 Provider 授权和模型目录。") from None
        if result != {
            "status": "passed",
            "model": result.get("model") if isinstance(result, dict) else None,
            "business_tools_enabled": False,
            "conversation_saved": False,
        } or not isinstance(result["model"], str):
            raise APIError(503, "probe_failed", "模型验收探针返回了无效结果。")
        return result

    def bind_chat_service(self, service, auth, *, bind_runtime_tool=True):
        """Bind the local product store after the Host owns its process lock."""
        store = getattr(service, "store", None)
        if (not callable(getattr(store, "get_conversation", None))
                or not callable(getattr(store, "list_messages", None))
                or not isinstance(getattr(auth, "server_id", None), str)):
            raise TypeError("Trading extension requires the local chat service and device authority")
        with self._order_guard:
            if ((self._chat_service is not None and self._chat_service is not service)
                    or (self._auth is not None and self._auth is not auth)):
                raise RuntimeError("Trading extension is already bound to another Host")
            if bind_runtime_tool and not self._tool_bound:
                bind_tool = getattr(service, "bind_turn_hooks", None)
                reauthorize = getattr(auth, "reauthorize", None)
                if not callable(bind_tool) or not callable(reauthorize):
                    raise RuntimeError("Trading Host does not support the model tool boundary")
                bind_tool(
                    interceptor=self._intercept_chat_turn,
                    runtime_tools_factory=self._runtime_tools_for_turn,
                )
                self._tool_bound = True
            self._chat_service, self._auth = service, auth
            if self._orders is None:
                order_store = self._order_store or PaperOrderStore(
                    Path(service.store.path).parent / "paper-orders.sqlite3")
                self._initialize_orders(order_store)
        return self.create_model_paper_draft

    def _intercept_chat_turn(self, *, history, caller):
        return self.confirm_chat_paper_order(
            history=history, caller=caller,
            reauthorize=lambda: self._auth.reauthorize(caller),
        )

    def _runtime_tools_for_turn(self, *, conversation_id, user_message_id, run_id,
                                caller, trusted_messages):
        return RuntimeToolContext(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            run_id=run_id,
            caller=caller,
            trusted_messages=trusted_messages,
            create_paper_draft=self.create_model_paper_draft,
            reauthorize=lambda: self._auth.reauthorize(caller),
            read_trading_data=self.read_model_trading_data,
            cancel_paper_order=self.cancel_model_paper_order,
        )

    def orders(self, handler):
        # Direct test/embedded servers may replace an extension on one service;
        # production Host performs the one authoritative tool binding itself.
        self.bind_chat_service(handler.server.service, handler.server.auth, bind_runtime_tool=False)
        with self._order_guard:
            return self._orders

    def _initialize_orders(self, store):
        service = PaperOrderService(store, self.broker, risk_policy=self.risk_policy)
        service.recover_interrupted()
        self._orders = service

    def prepare_broker_recovery(self):
        """Read and stage private identities without performing restart writes.

        ``run_host`` owns the process lock.  The normal ``orders`` path calls
        ``recover_interrupted`` only after that lock exists.
        """
        with self._order_guard:
            if self._order_store is None or self.broker is None:
                return 0
            service = self._orders or PaperOrderService(self._order_store, self.broker)
            plan = service.recovery_plan()
            if not plan["active_order_refs"]:
                return 0
            stage = getattr(self.broker, "stage_order_recovery", None)
            if not callable(stage) or stage(plan) is not True:
                raise RuntimeError("Paper order recovery could not be staged safely")
            return len(plan["active_order_refs"])

    @staticmethod
    def _model_uuid(value):
        try:
            result = str(UUID(value))
        except (AttributeError, TypeError, ValueError):
            raise OrderError("invalid_order", 400) from None
        if result != value:
            raise OrderError("invalid_order", 400)
        return result

    @staticmethod
    def _model_caller(caller, reauthorize):
        try:
            PaperOrderService.require_mac(caller)
            current = reauthorize()
            PaperOrderService.require_mac(current)
            if current["id"] != caller["id"]:
                raise ValueError()
            return current
        except Exception:
            raise OrderError("permission_revoked", 403) from None

    def create_model_paper_draft(self, *, proposal, conversation_id, user_message_id,
                                 run_id, caller, reauthorize):
        """Create and What-if preview a draft from a saved Mac chat run.

        This is the sole model-facing trading capability.  It cannot confirm,
        submit or cancel, and it never chooses an account or order
        execution setting from model arguments.
        """
        if not callable(reauthorize):
            raise OrderError("permission_revoked", 403)
        caller = self._model_caller(caller, reauthorize)
        if not isinstance(proposal, dict) or set(proposal) != {
                "symbol", "side", "quantity", "limit_price"}:
            raise OrderError("invalid_order", 400)
        conversation_id = self._model_uuid(conversation_id)
        user_message_id = self._model_uuid(user_message_id)
        run_id = self._model_uuid(run_id)
        with self._order_guard:
            if self._chat_service is None or self._auth is None or self._orders is None:
                raise OrderError("paper_facade_unavailable", 409)
            conversation = self._chat_service.store.get_conversation(conversation_id)
            if conversation is None or conversation.get("archived"):
                raise OrderError("invalid_order", 400)
            source = next((message for message in self._chat_service.store.list_messages(conversation_id)
                           if message.get("id") == user_message_id), None)
            if (source is None or source.get("role") != "user" or source.get("run_id") != run_id
                    or not isinstance(source.get("content"), str) or not source["content"].strip()):
                raise OrderError("invalid_order", 400)
            try:
                configuration = self.broker.health()["configuration"]
                selection = configuration["config_id"]
            except Exception:
                raise OrderError("broker_not_ready") from None
            data = {"client_request_id": run_id, "account_selection": selection,
                    "contract": {"symbol": proposal["symbol"], "sec_type": "STK",
                                 "exchange": "SMART", "currency": "USD"},
                    "side": proposal["side"], "quantity": proposal["quantity"],
                    "limit_price": proposal["limit_price"], "tif": "DAY", "order_type": "LMT"}
            status, result = self._orders.operate("create", data, caller, self._auth.server_id,
                                                  authorize=reauthorize)
            if status != 201:
                error = result.get("error") if isinstance(result, dict) else None
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    raise OrderError(error["code"], status)
                raise OrderError("order_state_conflict")
            if not isinstance(result, dict) or not isinstance(result.get("order"), dict):
                raise OrderError("order_state_conflict")
            order = result["order"]
            preview_request = str(uuid5(UUID(run_id), "paper-preview"))
            preview_data = {"client_request_id": preview_request, "version": order["version"],
                            "content_hash": order["content_hash"]}
            preview_status, preview_result = self._orders.operate(
                "preview", preview_data, caller, self._auth.server_id,
                order_id=order["id"], authorize=reauthorize)
            if preview_status != 200 or not isinstance(preview_result, dict):
                error = preview_result.get("error") if isinstance(preview_result, dict) else None
                raise OrderError(error.get("code", "preview_failed") if isinstance(error, dict)
                                 else "preview_failed", preview_status)
            order = preview_result.get("order")
            if not isinstance(order, dict) or order.get("state") != "proposed" or not isinstance(order.get("preview"), dict):
                raise OrderError("preview_failed")
            with self._orders.store.transaction() as db:
                event = db.execute("SELECT 1 FROM paper_events WHERE request_id=? "
                                   "AND type='order.model_draft_created'", (run_id,)).fetchone()
                if event is None:
                    row = self._orders.store.order(db, order["id"], self._auth.server_id)
                    if row is None or row["state"] != "proposed":
                        raise OrderError("order_state_conflict")
                    self._orders.store.event(db, row, "order.model_draft_created", self._orders.now(), run_id,
                        {"conversation_id": conversation_id, "user_message_id": user_message_id, "run_id": run_id})
            preview = order["preview"]
            return {"status": "review_ready", "order_id": order["id"], "state": "proposed",
                    "what_if": preview["what_if"], "expires_at": preview["expires_at"]}

    @staticmethod
    def _confirmation_artifact(history):
        if (len(history) < 2 or history[-1].get("role") != "user"
                or re.fullmatch(r"\s*(?:确认|确认下单|确认提交)\s*[。！!]?(?:\s*)", history[-1].get("content", "")) is None):
            return None
        previous = history[-2]
        if previous.get("role") != "assistant" or previous.get("status") != "completed":
            return None
        blocks = re.findall(r"```paper-order\s*\n([\s\S]*?)\n```", previous.get("content", ""))
        if len(blocks) != 1:
            return None
        try:
            artifact = json.loads(blocks[0])
            if (not isinstance(artifact, dict) or artifact.get("type") != "paper_order_draft"
                    or artifact.get("state") != "proposed"):
                return None
            return str(UUID(artifact["order_id"])), previous.get("run_id")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def confirm_chat_paper_order(self, *, history, caller, reauthorize):
        """Consume one exact chat confirmation for the immediately preceding trusted review."""
        artifact = self._confirmation_artifact(history)
        if artifact is None:
            return None
        order_id, source_run = artifact
        try:
            caller = self._model_caller(caller, reauthorize)
            current_run = self._model_uuid(history[-1]["run_id"])
            conversation_id = self._model_uuid(history[-1]["conversation_id"])
            with self._order_guard:
                if self._orders is None or self._auth is None:
                    raise OrderError("paper_facade_unavailable")
                detail = self._orders.get(order_id, self._auth.server_id)
                order = detail["order"]
                with self._orders.store.read_only() as db:
                    event = db.execute(
                        "SELECT payload FROM paper_events WHERE order_id=? AND request_id=? "
                        "AND type='order.model_draft_created' ORDER BY seq DESC LIMIT 1",
                        (order_id, source_run)).fetchone()
                payload = json.loads(event["payload"]) if event else None
                if not isinstance(payload, dict) or payload.get("conversation_id") != conversation_id:
                    raise OrderError("confirmation_required")
                if order.get("state") != "proposed" or not isinstance(order.get("preview"), dict):
                    raise OrderError("order_state_conflict")
                confirm_data = {
                    "client_request_id": str(uuid5(UUID(current_run), "paper-confirm")),
                    "version": order["version"], "content_hash": order["content_hash"],
                    "preview_id": order["preview"]["id"], "expires_at": order["preview"]["expires_at"],
                    "user_action": "confirm_paper_order",
                }
                status, result = self._orders.operate(
                    "confirm", confirm_data, caller, self._auth.server_id,
                    order_id=order_id, authorize=reauthorize)
                if status != 200 or not isinstance(result, dict) or not isinstance(result.get("order"), dict):
                    raise OrderError((result.get("error") or {}).get("code", "confirmation_required"), status)
                confirmed = result["order"]
                submit_data = {
                    "client_request_id": str(uuid5(UUID(current_run), "paper-submit")),
                    "version": confirmed["version"], "content_hash": confirmed["content_hash"],
                    "confirmation_id": confirmed["confirmation"]["id"],
                    "user_action": "submit_paper_order",
                }
                status, result = self._orders.operate(
                    "submit", submit_data, caller, self._auth.server_id,
                    order_id=order_id, authorize=reauthorize)
                if status != 200 or not isinstance(result, dict) or not isinstance(result.get("order"), dict):
                    raise OrderError((result.get("error") or {}).get("code", "order_state_conflict"), status)
                submitted = result["order"]
            state = submitted["state"]
            if state in ("accepted", "working", "partial", "filled"):
                return (f"IBKR Paper 订单已提交。状态：{state}；订单 ID：{order_id}；"
                        f"{submitted['side']} {submitted['quantity']} 股 {submitted['contract']['symbol']}，"
                        f"限价 {submitted['limit_price']} USD。")
            if state == "rejected":
                return f"IBKR Paper 已明确拒绝该订单。订单 ID：{order_id}。"
            return f"IBKR Paper 提交结果暂不明确，已停止自动重试。订单 ID：{order_id}，请先对账。"
        except OrderError as exc:
            return f"Paper 订单未提交：{exc.message}"

    def cancel_model_paper_order(self, *, order_id, conversation_id, user_message_id,
                                 run_id, caller, reauthorize):
        """Cancel one Paper order from an explicit, durable chat instruction."""
        caller = self._model_caller(caller, reauthorize)
        order_id = self._model_uuid(order_id)
        conversation_id = self._model_uuid(conversation_id)
        user_message_id = self._model_uuid(user_message_id)
        run_id = self._model_uuid(run_id)
        with self._order_guard:
            if self._orders is None or self._auth is None or self._chat_service is None:
                raise OrderError("paper_facade_unavailable")
            conversation = self._chat_service.store.get_conversation(conversation_id)
            source = next((message for message in self._chat_service.store.list_messages(conversation_id)
                           if message.get("id") == user_message_id), None)
            content = source.get("content") if isinstance(source, dict) else None
            if (conversation is None or conversation.get("archived") or source is None
                    or source.get("role") != "user" or source.get("run_id") != run_id
                    or not isinstance(content, str) or order_id not in content
                    or re.search(r"(?:撤(?:销)?(?:订单|单)|取消(?:订单|挂单)?|cancel)",
                                 content, re.IGNORECASE) is None):
                raise OrderError("invalid_order", 400)
            detail = self._orders.get(order_id, self._auth.server_id)
            order = detail["order"]
            data = {
                "client_request_id": str(uuid5(UUID(run_id), "paper-cancel:" + order_id)),
                "version": order["version"], "content_hash": order["content_hash"],
                "user_action": "cancel_paper_order",
            }
            status, result = self._orders.operate(
                "cancel", data, caller, self._auth.server_id,
                order_id=order_id, authorize=reauthorize)
            if (status != 200 or not isinstance(result, dict)
                    or not isinstance(result.get("order"), dict)):
                error = result.get("error") if isinstance(result, dict) else None
                raise OrderError(error.get("code", "order_state_conflict")
                                 if isinstance(error, dict) else "order_state_conflict", status)
            cancelled = result["order"]
            with self._orders.store.transaction() as db:
                existing = db.execute(
                    "SELECT 1 FROM paper_events WHERE order_id=? AND request_id=? "
                    "AND type='order.model_cancel_requested'",
                    (order_id, data["client_request_id"])).fetchone()
                if existing is None:
                    row = self._orders.store.order(db, order_id, self._auth.server_id)
                    self._orders.store.event(
                        db, row, "order.model_cancel_requested", self._orders.now(),
                        data["client_request_id"], {
                            "conversation_id": conversation_id,
                            "user_message_id": user_message_id,
                            "run_id": run_id,
                            "device_id": caller["id"],
                        })
            return {
                "status": "cancel_requested", "order_id": order_id,
                "state": cancelled["state"],
                "symbol": cancelled["contract"]["symbol"],
                "filled_quantity": cancelled["filled_quantity"],
            }

    @staticmethod
    def _model_read_caller(caller, reauthorize):
        try:
            if not isinstance(caller, dict) or not isinstance(caller.get("id"), str):
                raise ValueError()
            current = reauthorize()
            if (not isinstance(current, dict) or current.get("id") != caller["id"]
                    or current.get("revoked_at") is not None
                    or current.get("platform") not in ("macos", "ios")):
                raise ValueError()
            return current
        except Exception:
            raise OrderError("permission_revoked", 403) from None

    def read_model_trading_data(self, *, tool, arguments, caller, reauthorize):
        """Run bounded, account-redacted IBKR reads for the current chat turn."""
        if not callable(reauthorize):
            raise OrderError("permission_revoked", 403)
        self._model_read_caller(caller, reauthorize)
        if self.broker is None or not isinstance(arguments, dict):
            raise OrderError("broker_not_ready")
        if tool == MARKET_QUOTE_TOOL and set(arguments) == {"symbol"}:
            return self.broker.market_quote(arguments["symbol"])
        if tool == POSITIONS_TOOL and not arguments:
            return self.broker.positions_detail()
        if tool == OPEN_ORDERS_TOOL and not arguments:
            return self.broker.open_orders_detail()
        if tool == OPEN_ORDER_TOOL and set(arguments) == {"broker_order_id"}:
            return self.broker.open_orders_detail(broker_order_id=arguments["broker_order_id"])
        if tool in (PAPER_ORDERS_TOOL, PAPER_ORDER_TOOL):
            with self._order_guard:
                if self._orders is None or self._auth is None:
                    raise OrderError("paper_facade_unavailable")
                if tool == PAPER_ORDERS_TOOL and not arguments:
                    result = self._orders.listing(self._auth.server_id)
                    orders = [self._model_order(row) for row in result["orders"]]
                    return {"orders": orders, "count": len(orders)}
                if tool == PAPER_ORDER_TOOL and set(arguments) == {"order_id"}:
                    detail = self._orders.get(self._model_uuid(arguments["order_id"]), self._auth.server_id)
                    return {"order": self._model_order(detail["order"]),
                            "events": [{key: event.get(key) for key in
                                        ("seq", "type", "state", "error_code", "created_at")}
                                       for event in detail["events"]]}
        raise OrderError("invalid_order", 400)

    @staticmethod
    def _model_order(order):
        state, error = order["state"], order.get("error_code")
        if state in ("draft", "proposed", "confirmed"):
            submission = "not_submitted"
        elif state in ("submitting", "unknown"):
            submission = "outcome_unknown"
        elif state == "rejected":
            submission = "submitted_rejected" if error == "broker_rejected" else "not_submitted"
        else:
            submission = "submitted"
        contract = order["contract"]
        return {
            "order_id": order["id"], "environment": "paper", "state": state,
            "submission_status": submission, "symbol": contract["symbol"],
            "side": order["side"], "quantity": order["quantity"],
            "limit_price": order["limit_price"], "tif": order["tif"],
            "order_type": order["order_type"], "filled_quantity": order["filled_quantity"],
            "average_fill_price": order["average_fill_price"], "error_code": error,
            "created_at": order["created_at"], "updated_at": order["updated_at"],
        }

    @staticmethod
    def order_admin(handler):
        caller = handler.reauthorize("devices:manage")
        if caller["platform"] != "macos" or caller.get("revoked_at"):
            raise APIError(403, "forbidden", "仅已配对且有效的 Mac 管理设备可操作 Paper 订单。")
        return caller

    def broker_health(self):
        if self.broker is not None:
            return self.broker.health()
        return {
            "state": "not_configured", "connected": False, "execution_enabled": False,
            "environment_verified": False, "data_complete": False,
            "blockers": [{"code": "not_configured", "message": "尚未选择 IBKR paper/live 环境。"}],
        }

    def public_health(self):
        broker = self.broker_health()
        try:
            snapshot(self.broker)
            enabled = True
        except BrokerBoundaryError:
            enabled = False
        configuration = broker.get("configuration") if isinstance(broker, dict) else None
        environment = configuration.get("environment") if isinstance(configuration, dict) else None
        label = "IBKR Paper" if environment == "paper" else "IBKR Live" if environment == "live" else "IBKR"
        local_connected = broker.get("gateway_connected", broker.get("connected")) is True
        upstream_connected = broker.get("broker_connected", broker.get("connected")) is True
        data_ready = (upstream_connected and broker.get("environment_verified") is True
                      and broker.get("data_complete") is True and broker.get("health_stale") is not True)
        checking = broker.get("state") in ("connecting", "recovering", "reconciliation_required")
        components = [
            {
                "id": "ibkr_gateway", "label": "IBKR 本地接口",
                "state": "ready" if local_connected else "checking" if checking else "unavailable",
                "detail": "端口已连接" if local_connected else "正在连接" if checking else "未连接",
            },
            {
                "id": "ibkr_upstream", "label": f"{label} 上游",
                "state": "ready" if upstream_connected else "checking" if checking else "unavailable",
                "detail": "已连接" if upstream_connected else "正在恢复" if checking else "已断开",
            },
            {
                "id": "ibkr_data", "label": "IBKR 数据",
                "state": "ready" if data_ready else "checking" if upstream_connected or checking else "unavailable",
                "detail": "已就绪" if data_ready else "正在重新核对" if upstream_connected or checking else "不可用",
            },
        ]
        return {"trade_execution_enabled": enabled, "broker": broker, "components": components}

    def route(self, handler, method, path, query, caller):
        if path == "/v1/paper-trading/authorization" or path.startswith("/v1/paper-trading/devices"):
            try:
                orders = self.orders(handler)
                current = handler.reauthorize("chat:read" if method == "GET" else "chat:write")
                if query or urlsplit(handler.path).query:
                    raise OrderError("invalid_order", 400)
                if path == "/v1/paper-trading/authorization" and method == "GET":
                    handler.send_json(200, {"authorization": orders.device_authorization(current)})
                    return True
                current = self.order_admin(handler)
                if path == "/v1/paper-trading/devices" and method == "GET":
                    devices = []
                    for device in handler.server.auth.devices(current):
                        if device["platform"] != "ios":
                            continue
                        devices.append({**orders.device_authorization(device), "device_name": device["name"],
                                        "revoked_at": device.get("revoked_at")})
                    handler.send_json(200, {"devices": devices})
                    return True
                parts = path.split("/")
                if len(parts) == 5 and method == "POST":
                    data = read_order_json(handler)
                    if set(data) != {"enabled"} or type(data["enabled"]) is not bool:
                        raise OrderError("invalid_order", 400)
                    device_id = identifier(parts[4])
                    target = next((device for device in handler.server.auth.devices(current) if device["id"] == device_id), None)
                    if target is None:
                        raise OrderError("invalid_order", 400)
                    current = self.order_admin(handler)
                    handler.send_json(200, {"authorization": orders.set_device_grant(current, target, data["enabled"])})
                    return True
                raise OrderError("invalid_order", 400)
            except OrderError as exc:
                raise APIError(exc.status, exc.code, exc.message) from None
        if path == "/v1/positions" or path.startswith("/v1/instruments"):
            url = urlsplit(handler.path)
            lengths = handler.headers.get_all("Content-Length", [])
            if (method != "GET" or handler.headers.get("Transfer-Encoding") or lengths not in ([], ["0"])
                    or url.path != path):
                raise APIError(400, "invalid_input", "交易数据读取只接受无请求体的 GET。")
            try:
                raw_query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
            except ValueError:
                raise APIError(400, "invalid_input", "交易数据查询参数无效。") from None
            handler.reauthorize("chat:read")
            parts = path.split("/")
            try:
                if path == "/v1/positions" and not url.query:
                    kind, result, expected_id = "positions", self.broker.positions_detail(), None
                elif path == "/v1/instruments/search" and set(raw_query) == {"q"} and len(raw_query["q"]) == 1:
                    q = raw_query["q"][0]
                    if not 1 <= len(q) <= 64 or q != q.strip() or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&'/-]*", q) is None:
                        raise APIError(400, "invalid_input", "股票搜索条件无效。")
                    kind, result, expected_id = "search", self.broker.search_instruments(q), None
                elif len(parts) == 5 and parts[4] == "quote" and not url.query and re.fullmatch(r"[1-9][0-9]{0,9}", parts[3]):
                    expected_id = int(parts[3])
                    if expected_id > 2147483647:
                        raise APIError(400, "invalid_input", "合约 ID 无效。")
                    kind, result = "quote", self.broker.instrument_quote(expected_id)
                else:
                    raise APIError(400, "invalid_input", "交易数据路径或查询参数无效。")
                response = public_read(kind, result, expected_id=expected_id)
                handler.reauthorize("chat:read")
                handler.send_json(200, response)
                return True
            except APIError:
                raise
            except Exception:
                raise APIError(409, "trading_data_unavailable", "券商数据不可用或未通过校验，请稍后重试。") from None
        if path == "/v1/admin/full-test/simulator-session" and method == "POST":
            if query or handler.read_json():
                raise APIError(400, "invalid_input", "Simulator 验收会话只接受空 JSON 对象。")
            current = handler.reauthorize("devices:manage")
            if current["platform"] != "macos":
                raise APIError(403, "forbidden", "仅已配对的 Mac 管理设备可创建 Simulator 验收会话。")
            handler.send_json(201, {
                "session": self._issue_simulator_probe_session(handler.server.auth),
            })
            return True
        if path == "/v1/full-test/model-probe" and method == "POST":
            if query:
                raise APIError(400, "invalid_input", "模型验收探针不接受查询参数。")
            data = handler.read_json()
            if set(data) != {"grant"}:
                raise APIError(400, "invalid_input", "模型验收探针需要单次 Simulator 授权。")
            current = handler.reauthorize("chat:read")
            if current["platform"] != "ios":
                raise APIError(403, "forbidden", "此入口仅允许已配对的 iOS Simulator 设备。")
            self._consume_simulator_probe_grant(text_field(data, "grant", 1024))
            handler.send_json(200, {
                "probe": self._run_full_test_model_probe(handler.server.service),
            })
            return True
        if path == "/v1/admin/full-test/model-probe" and method == "POST":
            if query or handler.read_json():
                raise APIError(400, "invalid_input", "模型验收探针只接受空 JSON 对象。")
            current = handler.reauthorize("devices:manage")
            if current["platform"] != "macos":
                raise APIError(403, "forbidden", "仅已配对的 Mac 管理设备可运行模型验收探针。")
            handler.send_json(200, {
                "probe": self._run_full_test_model_probe(handler.server.service),
            })
            return True
        if path == "/v1/broker/health" and method == "GET":
            handler.send_json(200, {"broker": self.broker_health()})
            return True
        if path == "/v1/orders" or path.startswith("/v1/orders/"):
            parts = path.split("/")
            try:
                if query or urlsplit(handler.path).query:
                    raise OrderError("invalid_order", 400)
                user_id = handler.server.auth.server_id  # v1 has one local owner, shared across devices.
                if path == "/v1/orders" and method == "GET":
                    handler.send_json(200, self.orders(handler).listing(user_id))
                    return True
                if len(parts) == 4 and method == "GET":
                    handler.send_json(200, self.orders(handler).get(identifier(parts[3]), user_id))
                    return True
                if method == "POST" and (path == "/v1/orders" or (len(parts) == 5 and parts[4] in ("preview", "confirm", "submit", "cancel"))):
                    data = read_order_json(handler)
                    caller = handler.reauthorize("chat:write")
                    if data.get("environment") == "live":
                        raise OrderError("paper_only", 403)
                    action = "create" if path == "/v1/orders" else parts[4]
                    status, result = self.orders(handler).operate(action, data, caller, user_id,
                        order_id=identifier(parts[3]) if action != "create" else None,
                        authorize=lambda: handler.reauthorize("chat:write"))
                    handler.send_json(status, result)
                    return True
                raise APIError(403, "trade_execution_disabled", "不开放订单修改、批量撤单、定时单或 Live 操作。")
            except OrderError as exc:
                raise APIError(exc.status, exc.code, exc.message) from None
        if path.startswith(("/v1/orders", "/v1/approvals", "/v1/automations", "/v1/trades")):
            raise APIError(403, "trade_execution_disabled", "首版不开放交易执行、确认或交易预授权接口。")
        return False
