"""Trading-Agent-only additions to the shared Yolo Agents chat API."""

import json
from pathlib import Path
import threading

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
        self._order_guard = threading.Lock()

    def orders(self, handler):
        with self._order_guard:
            if self._orders is None:
                store = self._order_store or PaperOrderStore(Path(handler.server.service.store.path).parent / "paper-orders.sqlite3")
                self._initialize_orders(store)
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
