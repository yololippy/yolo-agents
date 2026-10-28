"""Paper-only order domain. No model tools, SDK calls, retries or live path."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from uuid import UUID, uuid4

from .paper_order_broker import BrokerBoundaryError, snapshot
from .paper_order_store import PaperOrderStore, encoded


ACTIVE = ("submitting", "unknown", "accepted", "working", "partial", "cancel_pending")
RESULT_STATES = {"accepted", "working", "partial", "filled", "rejected", "cancel_pending", "cancelled", "unknown"}
TERMINAL = {"filled", "rejected", "cancelled"}


class OrderError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        self.message = {
            "invalid_order": "订单字段无效；仅支持 Paper 股票 SMART/USD DAY 限价单。",
            "paper_only": "仅允许显式配置并核验的 Paper 环境。",
            "paper_facade_unavailable": "Paper 订单门面尚未配置，不能预览或执行。",
            "broker_not_ready": "券商账户、权限、连接或完整数据未满足要求。",
            "account_selection_mismatch": "账户选择与服务端配置目标不一致。",
            "version_conflict": "订单版本或内容已变化，请重新读取并预览。",
            "confirmation_required": "需要同一 Mac 对当前预览作出明确确认。",
            "confirmation_expired": "预览或确认已过期，请重新创建并预览订单。",
            "intent_in_flight": "相同订单意图已有未结束或结果不明的请求，禁止再次提交。",
            "idempotency_conflict": "请求标识已被其他内容、设备或操作使用。",
            "order_state_conflict": "当前订单状态不允许此操作。",
            "preview_failed": "合约或 what-if 预览未完整通过；未提交订单。",
            "broker_outcome_unknown": "券商操作结果不明；禁止自动重发，请先对账。",
            "permission_revoked": "设备权限已失效，未发起本次券商操作。",
            "order_not_found": "订单不存在。",
        }.get(code, "订单操作未完成，请检查订单状态。")
        super().__init__(self.message)


def uuid_text(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise OrderError("invalid_order", 400) from None


def digest(value):
    try:
        return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise OrderError("invalid_order", 400) from None


def money(value, *, signed=False):
    expression = r"-?(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,4})?" if signed else r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,4})?"
    if not isinstance(value, str) or re.fullmatch(expression, value) is None:
        raise OrderError("invalid_order", 400)
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise OrderError("invalid_order", 400) from None
    if not signed and not 0 < number <= Decimal("1000000"):
        raise OrderError("invalid_order", 400)
    return format(number.normalize(), "f")


def exact(data, fields):
    if not isinstance(data, dict) or set(data) != set(fields):
        raise OrderError("invalid_order", 400)


def intent_from(data):
    exact(data, {"client_request_id", "account_selection", "contract", "side", "quantity", "limit_price", "tif", "order_type"})
    contract = data["contract"]
    exact(contract, {"symbol", "sec_type", "exchange", "currency"})
    if (contract["sec_type"] != "STK" or contract["exchange"] != "SMART" or contract["currency"] != "USD"
            or not isinstance(contract["symbol"], str) or re.fullmatch(r"[A-Z][A-Z0-9.]{0,14}", contract["symbol"]) is None
            or data["side"] not in ("BUY", "SELL") or data["tif"] != "DAY" or data["order_type"] != "LMT"
            or type(data["quantity"]) is not int or not 1 <= data["quantity"] <= 100000
            or not isinstance(data["account_selection"], str)
            or re.fullmatch(r"[0-9a-f]{64}", data["account_selection"]) is None):
        raise OrderError("invalid_order", 400)
    return {key: (money(data[key]) if key == "limit_price" else data[key]) for key in data if key != "client_request_id"}


def public_order(row):
    intent = json.loads(row["intent"])
    preview = json.loads(row["preview"]) if row["preview"] else None
    confirmation = json.loads(row["confirmation"]) if row["confirmation"] else None
    return {"id": row["id"], "client_request_id": row["client_request_id"], "environment": "paper",
            **intent, "version": row["version"], "content_hash": row["content_hash"], "state": row["state"],
            "preview": ({key: preview[key] for key in ("id", "qualified_contract", "what_if", "expires_at")} if preview else None),
            "confirmation": ({key: confirmation[key] for key in ("id", "version", "content_hash", "expires_at")} if confirmation else None),
            "filled_quantity": row["filled_quantity"], "average_fill_price": row["average_fill_price"],
            "error_code": row["error_code"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


class PaperOrderService:
    def __init__(self, store: PaperOrderStore, broker, *, clock=None):
        self.store, self.broker = store, broker
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        return self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _expired(self, value):
        return self.clock().astimezone(timezone.utc) >= datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def require_mac(caller):
        if (caller.get("platform") != "macos" or caller.get("revoked_at")
                or "devices:manage" not in caller.get("permissions", ())):
            raise OrderError("permission_revoked", 403)

    def _context(self, selection):
        try:
            return snapshot(self.broker, selection)
        except BrokerBoundaryError as exc:
            raise OrderError(str(exc), 403 if str(exc) == "paper_only" else 409) from None

    def _draft_target(self, selection):
        try:
            configuration = self.broker.health()["configuration"]
            if configuration.get("environment") != "paper":
                raise OrderError("paper_only", 403)
            if configuration.get("account_configured") is not True or configuration.get("config_id") != selection:
                raise OrderError("account_selection_mismatch")
        except OrderError:
            raise
        except Exception:
            raise OrderError("broker_not_ready") from None

    def _get(self, db, order_id, user_id):
        row = self.store.order(db, order_id, user_id)
        if row is None:
            raise OrderError("order_not_found", 404)
        return row

    def get(self, order_id, user_id):
        self.refresh_cached_updates()
        with self.store.transaction() as db:
            row = self._get(db, uuid_text(order_id), user_id)
            view = public_order(row)
            events = db.execute("SELECT seq,type,state,error_code,created_at FROM paper_events "
                                "WHERE order_id=? ORDER BY seq LIMIT 500", (row["id"],)).fetchall()
            return {"order": view, "events": [dict(event) for event in events]}

    def listing(self, user_id, limit=100):
        self.refresh_cached_updates()
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM paper_orders WHERE user_id=? ORDER BY created_at DESC,id LIMIT ?",
                              (user_id, limit)).fetchall()
            return {"orders": [public_order(dict(row)) for row in rows]}

    def recover_interrupted(self):
        """Call once after the Host owns its single-instance lock. Never replay."""
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM paper_orders WHERE state IN ('submitting','cancel_pending','previewing')").fetchall()
            for saved in rows:
                row = dict(saved)
                preview_only = row["state"] == "previewing"
                row.update(state="draft" if preview_only else "unknown", error_code="preview_failed" if preview_only else "broker_outcome_unknown",
                           updated_at=self.now())
                self.store.save(db, row)
                self.store.event(db, row, "order.recovered", self.now(), error_code=row["error_code"])
            requests = db.execute("SELECT * FROM paper_requests WHERE status='in_progress'").fetchall()
            for request in requests:
                row = self.store.order(db, request["order_id"], request["user_id"])
                if row:
                    self._finish(db, request["id"], row, 200, {"order": public_order(row)})

    def _finish(self, db, request_id, row, status=200, response=None, error=None):
        response = response or {"order": public_order(row)}
        db.execute("UPDATE paper_requests SET status='completed',http_status=?,response=?,error_code=?,updated_at=? WHERE id=?",
                   (status, encoded(response), error, self.now(), request_id))
        return status, response

    def _failure(self, db, request_id, row, error):
        self.store.event(db, row, "request.failed", self.now(), request_id, error_code=error.code)
        return self._finish(db, request_id, row, error.status,
                            {"error": {"code": error.code, "message": error.message}}, error.code)

    def _version(self, row, data):
        if type(data.get("version")) is not int or data["version"] != row["version"] or data.get("content_hash") != row["content_hash"]:
            raise OrderError("version_conflict")

    def _unique_intent(self, db, row):
        other = db.execute("SELECT id FROM paper_orders WHERE user_id=? AND intent_key=? AND id<>? "
                           "AND state IN ('submitting','unknown','accepted','working','partial','cancel_pending') LIMIT 1",
                           (row["user_id"], row["intent_key"], row["id"])).fetchone()
        if other:
            raise OrderError("intent_in_flight")

    def operate(self, action, data, caller, user_id, *, order_id=None, authorize=None):
        self.require_mac(caller)
        if not isinstance(data, dict):
            raise OrderError("invalid_order", 400)
        request_id = uuid_text(data.get("client_request_id"))
        input_hash = digest(data)
        order_id = uuid_text(order_id) if order_id is not None else None
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM paper_requests WHERE id=?", (request_id,)).fetchone()
            if existing:
                if (existing["operation"] != action or existing["input_hash"] != input_hash or existing["device_id"] != caller["id"]
                        or existing["user_id"] != user_id or (action != "create" and existing["order_id"] != order_id)):
                    raise OrderError("idempotency_conflict")
                if existing["status"] == "completed":
                    return existing["http_status"], json.loads(existing["response"])
                row = self._get(db, existing["order_id"], user_id)
                return 202, {"order": public_order(row)}
            db.execute("INSERT INTO paper_requests(id,order_id,operation,input_hash,device_id,user_id,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,'in_progress',?,?)", (request_id, None, action, input_hash, caller["id"], user_id, self.now(), self.now()))
            row = None
            try:
                if authorize is not None:
                    self._reauthorize(authorize, caller)
                if action == "create":
                    intent = intent_from(data)
                    self._draft_target(intent["account_selection"])
                    row = {"id": str(uuid4()), "user_id": user_id, "client_request_id": request_id, "intent": encoded(intent),
                           # Conservatively block the same economic intent across
                           # profile changes while an older outcome is uncertain.
                           "intent_key": digest({key: value for key, value in intent.items() if key != "account_selection"}),
                           "content_hash": digest(intent), "version": 1, "state": "draft",
                           "preview": None, "confirmation": None, "broker_order_id": None, "filled_quantity": 0,
                           "average_fill_price": None, "error_code": None, "created_at": self.now(), "updated_at": self.now()}
                    self._unique_intent(db, row)
                    db.execute("INSERT INTO paper_orders(" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", list(row.values()))
                    db.execute("UPDATE paper_requests SET order_id=? WHERE id=?", (row["id"], request_id))
                    db.execute("INSERT INTO paper_versions VALUES (?,?,?,?,?)", (row["id"], 1, row["content_hash"], row["intent"], self.now()))
                    self.store.event(db, row, "order.created", self.now(), request_id)
                    return self._finish(db, request_id, row, 201)
                row = self._get(db, order_id, user_id)
                db.execute("UPDATE paper_requests SET order_id=? WHERE id=?", (order_id, request_id))
                intent = json.loads(row["intent"])
                if action == "preview":
                    exact(data, {"client_request_id", "version", "content_hash"})
                    self._version(row, data)
                    if row["state"] not in ("draft", "proposed"):
                        raise OrderError("order_state_conflict")
                    context = self._context(intent["account_selection"])
                    row.update(state="previewing", preview=None, confirmation=None, error_code=None)
                elif action == "confirm":
                    exact(data, {"client_request_id", "version", "content_hash", "preview_id", "expires_at", "user_action"})
                    self._version(row, data)
                    preview = json.loads(row["preview"]) if row["preview"] else None
                    if (row["state"] != "proposed" or not preview or data["preview_id"] != preview["id"]
                            or data["expires_at"] != preview["expires_at"] or data["user_action"] != "confirm_paper_order"):
                        raise OrderError("confirmation_required")
                    if self._expired(preview["expires_at"]):
                        raise OrderError("confirmation_expired")
                    if self._context(intent["account_selection"]) != preview["context"]:
                        raise OrderError("version_conflict")
                    if authorize is not None:
                        self._reauthorize(authorize, caller)
                    if self._expired(preview["expires_at"]):
                        raise OrderError("confirmation_expired")
                    confirmation = {"id": str(uuid4()), "version": row["version"], "content_hash": row["content_hash"],
                                    "preview_id": preview["id"], "device_id": caller["id"], "user_id": user_id,
                                    "user_action": data["user_action"], "expires_at": preview["expires_at"], "created_at": self.now()}
                    db.execute("INSERT INTO paper_confirmations VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (confirmation["id"], row["id"], row["version"], row["content_hash"], preview["id"], caller["id"],
                                user_id, data["user_action"], preview["expires_at"], self.now()))
                    row.update(state="confirmed", confirmation=encoded(confirmation), error_code=None, updated_at=self.now())
                    self.store.save(db, row)
                    self.store.event(db, row, "order.confirmed", self.now(), request_id,
                                     {"version": row["version"], "content_hash": row["content_hash"], "device_id": caller["id"],
                                      "user_action": data["user_action"], "expires_at": preview["expires_at"]})
                    return self._finish(db, request_id, row)
                elif action == "submit":
                    exact(data, {"client_request_id", "version", "content_hash", "confirmation_id", "user_action"})
                    self._version(row, data)
                    confirmation = json.loads(row["confirmation"]) if row["confirmation"] else None
                    preview = json.loads(row["preview"]) if row["preview"] else None
                    if (row["state"] != "confirmed" or not confirmation or not preview or data["user_action"] != "submit_paper_order"
                            or data["confirmation_id"] != confirmation["id"] or caller["id"] != confirmation["device_id"]
                            or confirmation["user_id"] != user_id or confirmation["content_hash"] != row["content_hash"]
                            or confirmation["version"] != row["version"]):
                        raise OrderError("confirmation_required")
                    if self._expired(confirmation["expires_at"]):
                        raise OrderError("confirmation_expired")
                    context = self._context(intent["account_selection"])
                    if context != preview["context"]:
                        raise OrderError("version_conflict")
                    self._unique_intent(db, row)
                    row.update(state="submitting", error_code=None)
                elif action == "cancel":
                    exact(data, {"client_request_id", "version", "content_hash", "user_action"})
                    self._version(row, data)
                    if data["user_action"] != "cancel_paper_order":
                        raise OrderError("invalid_order", 400)
                    if row["state"] in TERMINAL or row["state"] == "cancel_pending":
                        return self._finish(db, request_id, row)
                    if row["state"] not in ("accepted", "working", "partial") or not row["broker_order_id"]:
                        raise OrderError("order_state_conflict")
                    preview = json.loads(row["preview"])
                    context = self._context(intent["account_selection"])
                    if context != preview["context"]:
                        raise OrderError("version_conflict")
                    row.update(state="cancel_pending", error_code=None)
                else:
                    raise OrderError("invalid_order", 400)
                row["updated_at"] = self.now()
                self.store.save(db, row)
                self.store.event(db, row, "order." + row["state"], self.now(), request_id)
            except OrderError as error:
                return self._failure(db, request_id, row if row and self.store.order(db, row["id"], user_id) else None, error)
        # The request and transition are durable BEFORE any lifecycle operation.
        try:
            if self._context(intent["account_selection"]) != context:
                raise OrderError("version_conflict")
            if authorize is not None:
                self._reauthorize(authorize, caller)
            if action == "submit" and self._expired(confirmation["expires_at"]):
                raise OrderError("confirmation_expired")
        except OrderError as error:
            with self.store.transaction() as db:
                row = self._get(db, row["id"], user_id)
                row.update(state=row["state"] if row["state"] in TERMINAL else {"preview": "draft", "submit": "confirmed", "cancel": "unknown"}[action],
                           error_code=error.code, updated_at=self.now())
                self.store.save(db, row)
                return self._failure(db, request_id, row, error)
        try:
            if action == "preview":
                result = self.broker.preview_paper_order(intent, order_ref=row["id"], context=context)
                preview = self._preview_result(result, intent, context)
            elif action == "submit":
                result = self.broker.submit_paper_order(intent, order_ref=row["id"], preview=preview, context=context)
            else:
                result = self.broker.cancel_paper_order(order_ref=row["id"], broker_order_id=row["broker_order_id"], context=context)
        except Exception:
            if action == "preview":
                return self._preview_failure(row["id"], user_id, request_id)
            result = {"order_ref": row["id"], "status": "unknown"}
        with self.store.transaction() as db:
            row = self._get(db, row["id"], user_id)
            if action == "preview":
                row.update(state="proposed", version=row["version"] + 1, preview=encoded(preview), confirmation=None,
                           content_hash=digest({"intent": intent, "preview": preview}), error_code=None, updated_at=self.now())
                self.store.save(db, row)
                db.execute("INSERT INTO paper_versions VALUES (?,?,?,?,?)", (row["id"], row["version"], row["content_hash"],
                           encoded({"intent": intent, "preview": preview}), self.now()))
            else:
                self._merge_result(row, result)
                self.store.save(db, row)
            self.store.event(db, row, "order." + row["state"], self.now(), request_id, error_code=row["error_code"])
            return self._finish(db, request_id, row)

    def _reauthorize(self, authorize, original):
        try:
            current = authorize()
            self.require_mac(current)
            if current["id"] != original["id"]:
                raise OrderError("permission_revoked", 403)
        except Exception:
            raise OrderError("permission_revoked", 403) from None

    def _preview_failure(self, order_id, user_id, request_id):
        with self.store.transaction() as db:
            row = self._get(db, order_id, user_id)
            row.update(state="draft", preview=None, confirmation=None, error_code="preview_failed", updated_at=self.now())
            self.store.save(db, row)
            return self._failure(db, request_id, row, OrderError("preview_failed", 409))

    def _preview_result(self, result, intent, context):
        if not isinstance(result, dict):
            raise OrderError("preview_failed")
        contract, what_if = result.get("qualified_contract"), result.get("what_if")
        if (not isinstance(contract, dict) or type(contract.get("con_id")) is not int or contract["con_id"] <= 0
                or any(contract.get(key) != value for key, value in intent["contract"].items())
                or not isinstance(what_if, dict) or what_if.get("complete") is not True
                or what_if.get("accepted") is not True or what_if.get("currency") != "USD"):
            raise OrderError("preview_failed")
        amount_keys = ("initial_margin_change", "maintenance_margin_change", "commission")
        for key in amount_keys:
            money(what_if.get(key), signed=True)
        # Preserve the facade's validated decimal spelling: its owner-loop
        # preview binding may compare these exact values at submission.
        amounts = {key: what_if[key] for key in amount_keys}
        if Decimal(amounts["commission"]) < 0:
            raise OrderError("preview_failed")
        return {"id": str(uuid4()), "qualified_contract": {**intent["contract"], "con_id": contract["con_id"]},
                "what_if": {"complete": True, "accepted": True, "currency": "USD", **amounts}, "context": context,
                "expires_at": (self.clock().astimezone(timezone.utc) + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")}

    def _merge_result(self, row, result):
        """Fill quantities only increase. Late cancel acknowledgements cannot undo fills."""
        quantity = json.loads(row["intent"])["quantity"]
        try:
            if not isinstance(result, dict) or result.get("order_ref") != row["id"] or result.get("status") not in RESULT_STATES:
                raise ValueError()
            state, filled = result["status"], result.get("filled_quantity", row["filled_quantity"])
            broker_id = result.get("broker_order_id", row["broker_order_id"])
            if type(filled) is not int or not 0 <= filled <= quantity:
                raise ValueError()
            if broker_id is not None and (not isinstance(broker_id, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", broker_id) is None):
                raise ValueError()
            if row["broker_order_id"] and broker_id != row["broker_order_id"]:
                raise ValueError()
            if state != "unknown" and not broker_id:
                raise ValueError()
            if (state == "filled" and filled != quantity) or (state == "partial" and not 0 < filled < quantity):
                raise ValueError()
            if state == "rejected" and filled:
                raise ValueError()
            average = result.get("average_fill_price")
            average = money(average) if average is not None else row["average_fill_price"]
        except (OrderError, ValueError, TypeError):
            state, filled, broker_id, average = "unknown", row["filled_quantity"], row["broker_order_id"], row["average_fill_price"]
        if state == "unknown":
            # An ambiguous result cannot turn its unverified fill numbers into a
            # terminal acknowledgement. Preserve only previously committed fills.
            filled, average = row["filled_quantity"], row["average_fill_price"]
        if filled < row["filled_quantity"] or row["state"] == "filled":
            average = row["average_fill_price"]
        filled = max(filled, row["filled_quantity"])
        if row["state"] == "filled":
            state = "filled"
        elif filled == quantity and state != "unknown":
            state = "filled"
        elif row["state"] in ("cancelled", "rejected"):
            state = row["state"]
        elif row["state"] == "cancel_pending" and state in ("accepted", "working", "partial"):
            state = "cancel_pending"
        elif filled and state in ("accepted", "working"):
            state = "partial"
        row.update(state=state, filled_quantity=filled, broker_order_id=broker_id, average_fill_price=average,
                   error_code="broker_outcome_unknown" if state == "unknown" else "broker_rejected" if state == "rejected" else None,
                   updated_at=self.now())

    def apply_broker_update(self, order_ref, result):
        """Trusted lifecycle reconciliation entry; never exposed as a client/model API.

        The lifecycle must match the configured account, orderRef, client ownership
        and broker order ID before publishing. This does not retry any operation.
        """
        with self.store.transaction() as db:
            saved = db.execute("SELECT * FROM paper_orders WHERE id=?", (uuid_text(order_ref),)).fetchone()
            if saved is None:
                raise OrderError("order_not_found", 404)
            row = dict(saved)
            if row["state"] not in ACTIVE + ("filled", "cancelled", "rejected"):
                raise OrderError("order_state_conflict")
            keys = ("state", "filled_quantity", "average_fill_price", "broker_order_id", "error_code")
            before = tuple(row[key] for key in keys)
            self._merge_result(row, result)
            if tuple(row[key] for key in keys) == before:
                return public_order(dict(saved))
            self.store.save(db, row)
            self.store.event(db, row, "order." + row["state"], self.now(), error_code=row["error_code"])
            return public_order(row)

    def refresh_cached_updates(self):
        """Consume only copied owner-loop records; never initiate SDK requests.

        Cached facts must name a known orderRef and the exact configuration /
        session from its preview. No client endpoint accepts these records.
        """
        source = getattr(self.broker, "orders_snapshot", None)
        if not callable(source):
            return
        try:
            records = source()
        except Exception:
            return  # Do not fabricate acknowledgements when the cache is unavailable.
        if not isinstance(records, list):
            return
        with self.store.transaction() as db:
            for result in records[:1000]:
                if not isinstance(result, dict) or not isinstance(result.get("order_ref"), str):
                    continue
                saved = db.execute("SELECT * FROM paper_orders WHERE id=?", (result["order_ref"],)).fetchone()
                if saved is None:
                    continue
                row = dict(saved)
                if not row["preview"] or row["state"] not in ACTIVE + ("filled", "cancelled", "rejected"):
                    continue
                context = json.loads(row["preview"])["context"]
                if result.get("config_id") != context["config_id"] or result.get("session_id") != context["session_id"]:
                    continue
                keys = ("state", "filled_quantity", "average_fill_price", "broker_order_id", "error_code")
                before = tuple(row[key] for key in keys)
                self._merge_result(row, result)
                if tuple(row[key] for key in keys) != before:
                    self.store.save(db, row)
                    self.store.event(db, row, "order." + row["state"], self.now(), error_code=row["error_code"])
