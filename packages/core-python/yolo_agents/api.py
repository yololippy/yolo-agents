"""Versioned HTTP/SSE API using only Python's standard library."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from .auth import AuthError


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise APIError(400, "invalid_identifier", "标识必须是 UUID。") from None


def text_field(data, field, maximum=200, optional=False):
    value = data.get(field)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise APIError(400, "invalid_input", f"{field} 必须是非空文本，长度不超过 {maximum}。")
    return value.strip()


class ChatHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, service, auth, extension=None):
        self.service, self.auth, self.extension = service, auth, extension
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "YoloAgents/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        # Never log Authorization, pairing codes, message text, or query strings.
        pass

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "invalid_body", "不支持此请求体编码。")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "invalid_body", "请求体长度无效。") from None
        if size <= 0 or size > 65536:
            raise APIError(413, "body_too_large", "请求体必须在 1 至 65536 字节之间。")
        if self.headers.get_content_type() != "application/json":
            raise APIError(415, "json_required", "请使用 application/json。")
        try:
            result = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            raise APIError(400, "invalid_json", "JSON 请求体无效。") from None
        if not isinstance(result, dict):
            raise APIError(400, "invalid_json", "请求体必须是 JSON 对象。")
        return result

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def reauthorize(self, permission="chat:write"):
        caller = self.server.auth.authenticate(self.headers.get("Authorization"))
        self.server.auth.require(caller, permission)
        return caller

    def dispatch(self, method):
        try:
            self.route(method)
        except (AuthError, APIError) as exc:
            self.close_connection = True
            self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except LookupError:
            self.close_connection = True
            self.send_json(404, {"error": {"code": "not_found", "message": "记录不存在。"}})
        except ValueError:
            self.close_connection = True
            self.send_json(409, {"error": {"code": "conflict", "message": "请求与已保存的数据冲突。"}})
        except (BrokenPipeError, ConnectionError, socket.timeout):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self.send_json(500, {"error": {"code": "internal_error", "message": "服务处理失败，请查看本机服务状态。"}})

    def route(self, method):
        url = urlsplit(self.path)
        path, query = url.path.rstrip("/"), parse_qs(url.query)
        app, auth = self.server.service, self.server.auth
        if method == "GET" and path == "/v1/health":
            health = {"status": "ok", "api_version": 1, "server_id": auth.server_id,
                      "agent_mode": app.runtime.mode, "model": app.runtime.model_name}
            if self.server.extension is not None:
                health.update(self.server.extension.public_health())
            self.send_json(200, health)
            return
        if method == "POST" and path == "/v1/devices/pair":
            data = self.read_json()
            result = auth.pair(text_field(data, "pairing_code"), text_field(data, "device_name", 100),
                               text_field(data, "platform", 10), self.client_address[0])
            self.send_json(201, result)
            return
        caller = auth.authenticate(self.headers.get("Authorization"))
        auth.require(caller, "chat:read" if method == "GET" else "chat:write")
        if self.server.extension is not None and self.server.extension.route(self, method, path, query, caller):
            return
        if path == "/v1/devices" and method == "GET":
            self.send_json(200, {"devices": auth.devices(caller)})
            return
        if path.startswith("/v1/devices/") and method == "DELETE":
            target = identifier(path.split("/")[-1])
            self.send_json(200, {"device": auth.revoke(caller, target)})
            return
        if path == "/v1/conversations":
            if method == "GET":
                archived = query.get("include_archived", ["false"])[0]
                if archived not in ("true", "false"):
                    raise APIError(400, "invalid_input", "include_archived 必须为 true 或 false。")
                search = query.get("query", [""])[0]
                if len(search) > 200:
                    raise APIError(400, "invalid_input", "搜索文本过长。")
                self.send_json(200, {"conversations": app.store.list_conversations(search, archived == "true")})
                return
            if method == "POST":
                title = text_field(self.read_json(), "title")
                self.reauthorize()
                conversation = app.store.create_conversation(title)
                self.send_json(201, {"conversation": conversation})
                return
        parts = path.split("/")
        if len(parts) in (4, 5) and parts[1:3] == ["v1", "conversations"]:
            conversation_id = identifier(parts[3])
            conversation = app.store.get_conversation(conversation_id)
            if not conversation:
                raise LookupError()
            if len(parts) == 4 and method == "PATCH":
                data = self.read_json()
                if not data or set(data) - {"title", "archived"}:
                    raise APIError(400, "invalid_input", "请提供 title 或 archived。")
                title = text_field(data, "title") if "title" in data else None
                archived = data.get("archived")
                if "archived" in data and not isinstance(archived, bool):
                    raise APIError(400, "invalid_input", "archived 必须为布尔值。")
                self.reauthorize()
                self.send_json(200, {"conversation": app.store.update_conversation(conversation_id, title, archived)})
                return
            if len(parts) == 5 and parts[4] == "messages":
                if method == "GET":
                    self.send_json(200, {"messages": app.store.list_messages(conversation_id)})
                    return
                if method == "POST":
                    data = self.read_json()
                    if set(data) - {"content", "client_message_id"}:
                        raise APIError(400, "invalid_input", "客户端只能提交消息文本和幂等标识。")
                    content = text_field(data, "content", 16000)
                    client_message_id = identifier(data.get("client_message_id"))
                    self.reauthorize()
                    self.send_json(202, app.send(conversation_id, content, client_message_id))
                    return
        if path == "/v1/events" and method == "GET":
            try:
                after = int(query.get("after", [self.headers.get("Last-Event-ID", "0")])[0])
                limit = int(query.get("limit", ["200"])[0])
            except ValueError:
                raise APIError(400, "invalid_cursor", "事件游标和数量必须为整数。") from None
            if not 0 <= after <= 9223372036854775807 or not 1 <= limit <= 500:
                raise APIError(400, "invalid_cursor", "事件游标或数量超出范围。")
            conversation_id = query.get("conversation_id", [None])[0]
            if conversation_id:
                conversation_id = identifier(conversation_id)
                if app.store.get_conversation(conversation_id) is None:
                    raise LookupError()
            streaming = query.get("stream", ["false"])[0] == "true" or "text/event-stream" in self.headers.get("Accept", "")
            if streaming:
                self.stream_events(after, limit, conversation_id)
            else:
                events = app.store.list_events(after, limit + 1, conversation_id)
                visible = events[:limit]
                self.send_json(200, {"events": visible, "next_cursor": visible[-1]["seq"] if visible else after,
                                     "has_more": len(events) > limit})
            return
        raise APIError(404, "not_found", "接口不存在。")

    def stream_events(self, after, limit, conversation_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b": connected\nretry: 2000\n\n")
        self.wfile.flush()
        deadline, heartbeat = time.monotonic() + 25, time.monotonic()
        while time.monotonic() < deadline and not self.server.service.closing.is_set():
            try:
                self.server.auth.authenticate(self.headers.get("Authorization"))
            except AuthError:
                return
            events = self.server.service.store.list_events(after, limit, conversation_id)
            for event in events:
                payload = json.dumps(event, ensure_ascii=False)
                frame = f"id: {event['seq']}\nevent: {event['type']}\ndata: {payload}\n\n"
                self.wfile.write(frame.encode("utf-8"))
                after = event["seq"]
            if time.monotonic() - heartbeat >= 5:
                self.wfile.write(b": heartbeat\n\n")
                heartbeat = time.monotonic()
            self.wfile.flush()
            self.server.service.closing.wait(0.2)
