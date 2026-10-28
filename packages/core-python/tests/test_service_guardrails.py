"""Guardrail failures through the real chat queue, SQLite and HTTP event API."""

import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from yolo_agents.agent_runtime import create_runtime
from yolo_agents.api import ChatHTTPServer
from yolo_agents.auth import DeviceAuth
from yolo_agents.service import ChatService
from yolo_agents.store import Store


SECRET = "sk-proj-" + "syntheticCredentialForOfflineTests1234567890"


class GuardrailHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        blocker = patch("pydantic_ai.models.ALLOW_MODEL_REQUESTS", False)
        blocker.start()
        self.addCleanup(blocker.stop)
        banner = patch.dict("os.environ", {"PYDANTIC_AI_NO_BANNER": "1"})
        banner.start()
        self.addCleanup(banner.stop)
        self.store = Store(self.root / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.requests = []
        self.output = ["普通回复。"]
        self.pause = 0
        self.finish_gate = None

        async def stream(messages, _info):
            self.requests.append(messages)
            for part in self.output:
                yield part
                if self.pause:
                    await asyncio.sleep(self.pause)
            if self.finish_gate is not None:
                deadline = time.monotonic() + 4
                while not self.finish_gate.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(.01)

        model = FunctionModel(stream_function=stream)
        self.runtime = create_runtime("pydantic", "openai:offline-placeholder", self.root, self.store)
        self.runtime._backend.Agent = lambda _model, **kwargs: Agent(model, **kwargs)
        self.addCleanup(self.runtime.close)
        self.app = ChatService(self.store, self.runtime)
        self.addCleanup(self.app.close)
        self.auth = DeviceAuth(self.root / "devices.sqlite3")
        self.server = ChatHTTPServer(("127.0.0.1", 0), self.app, self.auth)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=2)

        self.addCleanup(stop_server)
        self.token = None
        status, paired = self.request("POST", "/v1/devices/pair", {
            "pairing_code": self.auth.issue_pairing_code("macos"),
            "platform": "macos", "device_name": "Offline guardrail test",
        })
        self.assertEqual(status, 201)
        self.token = paired["token"]
        status, result = self.request("POST", "/v1/conversations", {"title": "Guardrail test"})
        self.assertEqual(status, 201)
        self.cid = result["conversation"]["id"]

    def request(self, method, path, data=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def send(self, content, message_id=None):
        return self.request("POST", f"/v1/conversations/{self.cid}/messages", {
            "content": content, "client_message_id": message_id or str(uuid4()),
        })

    def terminal(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, body = self.request("GET", f"/v1/events?conversation_id={self.cid}")
            self.assertEqual(status, 200)
            events = body["events"]
            if any(event["type"] in ("assistant.failed", "assistant.completed") for event in events):
                return events
            time.sleep(.01)
        self.fail("Chat queue did not produce a terminal event")

    def assert_blocked(self, events):
        terminal = [event for event in events if event["type"] in ("assistant.failed", "assistant.completed")]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["type"], "assistant.failed")
        payload = terminal[0]["payload"]
        self.assertEqual(payload["error"]["code"], "guardrail_blocked")
        self.assertEqual(payload["message"]["status"], "failed")
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertIsNone(self.store.get_agent_state(self.cid))

    def test_input_block_prevents_model_call_and_duplicate_does_not_retry(self):
        identity = str(uuid4())
        content = "请保存这个 API key：" + SECRET
        self.assertEqual(self.send(content, identity)[0], 202)
        self.assert_blocked(self.terminal())
        self.assertEqual(self.requests, [])
        # Chat history deliberately retains the user's original text. It must
        # not be confused with a guarantee of whole-database secret removal.
        self.assertEqual(self.store.list_messages(self.cid)[0]["content"], content)
        status, duplicate = self.send(content, identity)
        self.assertIn(status, (200, 202))
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(self.requests, [])
        self.assert_blocked(self.terminal())

    def test_split_output_secret_never_enters_assistant_event_stream(self):
        self.output = ["先介绍一下。\n", "s", "k-", "proj-", SECRET[len("sk-proj-"):]]
        self.pause = .06
        self.assertEqual(self.send("请介绍配置方法")[0], 202)
        events = self.terminal()
        self.assert_blocked(events)
        text = "".join(event["payload"]["delta"] for event in events if event["type"] == "assistant.delta")
        self.assertNotIn("sk-proj-", text)
        self.assertNotIn(SECRET[len("sk-proj-"):], text)
        self.assertEqual(len(self.requests), 1)

    def test_ordinary_chinese_market_and_key_explanations_still_complete(self):
        self.output = ["API key 是调用凭据。", "模拟账户 DU1234567 不代表实盘授权。"]
        self.pause = .06
        self.assertEqual(self.send("解释 API key、AAPL 和模拟账户；邮箱是 user@example.com")[0], 202)
        events = self.terminal()
        final = [event for event in events if event["type"] == "assistant.completed"]
        self.assertEqual(len(final), 1)
        self.assertFalse(any(event["type"] == "assistant.failed" for event in events))
        text = "".join(event["payload"]["delta"] for event in events if event["type"] == "assistant.delta")
        self.assertEqual(text, "".join(self.output))
        self.assertEqual(final[0]["payload"]["message"]["content"], text)

    def test_safe_text_arrives_before_model_finishes(self):
        self.output = ["这是普通的中文分析。\n" * 80]
        self.finish_gate = threading.Event()
        self.pause = .06
        try:
            self.assertEqual(self.send("请做一个普通分析")[0], 202)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                events = self.store.list_events(conversation_id=self.cid)
                if any(event["type"] == "assistant.delta" for event in events):
                    break
                time.sleep(.01)
            else:
                self.fail("Guardrails buffered the whole reply instead of streaming safe text")
            self.assertFalse(any(event["type"] == "assistant.completed" for event in events))
        finally:
            self.finish_gate.set()
        self.assertTrue(any(event["type"] == "assistant.completed" for event in self.terminal()))


if __name__ == "__main__":
    unittest.main()
