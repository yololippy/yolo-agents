"""Trading-Agent-only additions to the shared Yolo Agents chat API."""

import json
from pathlib import Path
import threading
from uuid import UUID

from yolo_agents.api import APIError, identifier

from .paper_order_store import PaperOrderStore
from .paper_order_broker import BrokerBoundaryError, snapshot
from .paper_orders import OrderError, PaperOrderService


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

    def __init__(self, broker=None, *, orders_path=None):
        self.broker = broker
        # A migration failure propagates to Host startup; never enables a fallback.
        self._order_store = PaperOrderStore(orders_path) if orders_path is not None else None
        self._orders = None
        self._order_guard = threading.RLock()
        self._chat_service = None
        self._auth = None
        self._tool_bound = False

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
                bind_tool = getattr(service, "bind_paper_draft_tool", None)
                reauthorize = getattr(auth, "reauthorize", None)
                if not callable(bind_tool) or not callable(reauthorize):
                    raise RuntimeError("Trading Host does not support the draft-only tool boundary")
                bind_tool(self.create_model_paper_draft, reauthorize)
                self._tool_bound = True
            self._chat_service, self._auth = service, auth
            if self._orders is None:
                order_store = self._order_store or PaperOrderStore(
                    Path(service.store.path).parent / "paper-orders.sqlite3")
                self._initialize_orders(order_store)
        return self.create_model_paper_draft

    def orders(self, handler):
        # Direct test/embedded servers may replace an extension on one service;
        # production Host performs the one authoritative tool binding itself.
        self.bind_chat_service(handler.server.service, handler.server.auth, bind_runtime_tool=False)
        with self._order_guard:
            return self._orders

    def _initialize_orders(self, store):
        service = PaperOrderService(store, self.broker)
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
        """Create only a draft from a saved Mac chat run.

        This is the sole model-facing trading capability.  It cannot preview,
        confirm, submit or cancel, and it never chooses an account or order
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
            with self._orders.store.transaction() as db:
                event = db.execute("SELECT 1 FROM paper_events WHERE request_id=? "
                                   "AND type='order.model_draft_created'", (run_id,)).fetchone()
                if event is None:
                    row = self._orders.store.order(db, order["id"], self._auth.server_id)
                    if row is None or row["state"] != "draft":
                        raise OrderError("order_state_conflict")
                    self._orders.store.event(db, row, "order.model_draft_created", self._orders.now(), run_id,
                        {"conversation_id": conversation_id, "user_message_id": user_message_id, "run_id": run_id})
            return {"status": "draft_created", "order_id": order["id"], "state": "draft"}

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
        try:
            snapshot(self.broker)
            enabled = True
        except BrokerBoundaryError:
            enabled = False
        return {"trade_execution_enabled": enabled, "broker": self.broker_health()}

    def route(self, handler, method, path, query, caller):
        if path == "/v1/broker/health" and method == "GET":
            handler.send_json(200, {"broker": self.broker_health()})
            return True
        if path == "/v1/orders" or path.startswith("/v1/orders/"):
            parts = path.split("/")
            if method != "GET":
                caller = self.order_admin(handler)
            try:
                if query:
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
                    caller = self.order_admin(handler)
                    if data.get("environment") == "live":
                        raise OrderError("paper_only", 403)
                    action = "create" if path == "/v1/orders" else parts[4]
                    status, result = self.orders(handler).operate(action, data, caller, user_id,
                        order_id=identifier(parts[3]) if action != "create" else None,
                        authorize=lambda: self.order_admin(handler))
                    handler.send_json(status, result)
                    return True
                raise APIError(403, "trade_execution_disabled", "不开放订单修改、批量撤单、定时单或 Live 操作。")
            except OrderError as exc:
                raise APIError(exc.status, exc.code, exc.message) from None
        if path.startswith(("/v1/orders", "/v1/approvals", "/v1/automations", "/v1/trades")):
            raise APIError(403, "trade_execution_disabled", "首版不开放交易执行、确认或交易预授权接口。")
        return False
