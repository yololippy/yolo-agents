"""Draft-only runtime tool contracts; no broker or network is used."""

import json
import unittest
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from yolo_agents.runtime_tools import (
    PAPER_DRAFT_SCHEMA,
    PAPER_DRAFT_TOOL,
    RuntimeToolContext,
    finalize_trading_reply,
    paper_draft_dynamic_tool,
    validate_paper_draft_arguments,
)
from yolo_agents.service import ChatService
from yolo_agents.store import Store


class RuntimeToolTests(unittest.TestCase):
    def context(self, callback):
        return RuntimeToolContext(
            conversation_id=str(uuid4()), user_message_id=str(uuid4()), run_id=str(uuid4()),
            caller={"id": str(uuid4()), "platform": "macos", "permissions": ["devices:manage"]},
            trusted_messages=({"role": "user", "content": "Buy two TSLA at limit 250.00"},),
            create_paper_draft=callback, reauthorize=lambda: {"platform": "macos"},
        )

    def test_schema_requires_explicit_limit_price_and_exposes_no_execution_actions(self):
        spec = paper_draft_dynamic_tool()
        self.assertEqual(spec["name"], PAPER_DRAFT_TOOL)
        self.assertEqual(set(PAPER_DRAFT_SCHEMA["required"]), {"symbol", "side", "quantity", "limit_price"})
        encoded = str(spec).casefold()
        for forbidden in ("confirm_paper_order", "submit_paper_order", "cancel_paper_order"):
            self.assertNotIn(forbidden, encoded)
        with self.assertRaises(ValueError):
            validate_paper_draft_arguments({"symbol": "TSLA", "side": "BUY", "quantity": 2})

    def test_valid_proposal_reaches_callback_with_trusted_context_only(self):
        calls = []
        order_id = str(uuid4())

        def create(**kwargs):
            calls.append(kwargs)
            self.assertEqual(kwargs["reauthorize"](), {"platform": "macos"})
            return {"status": "draft_created", "order_id": order_id, "state": "draft"}

        context = self.context(create)
        result = context.invoke(PAPER_DRAFT_TOOL, {
            "symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250.00",
        })
        self.assertEqual(result, {"status": "draft_created", "order_id": order_id, "state": "draft"})
        self.assertEqual(calls[0]["proposal"], {
            "symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250.00",
        })
        self.assertNotIn("account_selection", calls[0]["proposal"])

    def test_callback_errors_and_malformed_results_are_fixed_and_private(self):
        for callback in (
            lambda **_: (_ for _ in ()).throw(RuntimeError("private account and token")),
            lambda **_: {"status": "submitted", "order_id": str(uuid4()), "state": "accepted"},
        ):
            with self.subTest(callback=callback):
                result = self.context(callback).invoke(PAPER_DRAFT_TOOL, {
                    "symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250",
                })
                self.assertEqual(result["status"], "draft_unavailable")
                self.assertNotIn("private", str(result))

    def test_callback_exposes_only_an_allowlisted_application_error_code(self):
        class SafeError(Exception):
            code = "permission_revoked"

        class PrivateError(Exception):
            code = "private_account_failure"

        safe = self.context(lambda **_: (_ for _ in ()).throw(SafeError("secret"))).invoke(
            PAPER_DRAFT_TOOL,
            {"symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250"},
        )
        private = self.context(lambda **_: (_ for _ in ()).throw(PrivateError("secret"))).invoke(
            PAPER_DRAFT_TOOL,
            {"symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250"},
        )
        self.assertEqual(safe["error_code"], "permission_revoked")
        self.assertNotIn("error_code", private)
        self.assertNotIn("secret", str(safe) + str(private))

    def test_model_cannot_invent_a_missing_limit_price(self):
        calls = []
        context = self.context(lambda **kwargs: calls.append(kwargs))
        context.trusted_messages = ({"role": "user", "content": "Buy two shares of Tesla"},)
        result = context.invoke(PAPER_DRAFT_TOOL, {
            "symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250",
        })
        self.assertEqual(result["status"], "needs_information")
        self.assertEqual(result["missing_fields"], ["limit_price"])
        self.assertEqual(calls, [])
        self.assertIsNone(context.trusted_artifact())

    def test_numeric_reply_is_price_only_after_an_explicit_limit_question(self):
        order_id = str(uuid4())
        context = self.context(lambda **_: {
            "status": "draft_created", "order_id": order_id, "state": "draft",
        })
        context.trusted_messages = (
            {"role": "assistant", "content": "What limit price should I use?"},
            {"role": "user", "content": "250"},
        )
        result = context.invoke(PAPER_DRAFT_TOOL, {
            "symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250",
        })
        self.assertEqual(result["status"], "draft_created")

    def test_chinese_hang_order_phrase_is_an_explicit_limit_price(self):
        order_id = str(uuid4())
        context = self.context(lambda **_: {
            "status": "draft_created", "order_id": order_id, "state": "draft",
        })
        context.trusted_messages = (
            {"role": "user", "content": "帮我下单买两股特斯拉"},
            {"role": "assistant", "content": "请提供每股限价"},
            {"role": "user", "content": "挂 403 的单子"},
        )

        result = context.invoke(PAPER_DRAFT_TOOL, {
            "symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "403",
        })

        self.assertEqual(result, {"status": "draft_created", "order_id": order_id, "state": "draft"})

    def test_only_trusted_tool_result_can_create_final_draft_artifact(self):
        trusted_id, fake_id = str(uuid4()), str(uuid4())
        context = self.context(lambda **_: {
            "status": "draft_created", "order_id": trusted_id, "state": "draft",
        })
        forged = (
            "Created.\n```json\n"
            f'{{"type":"paper_order_draft","order_id":"{fake_id}"}}\n```\n'
            "```paper-order\n{\"type\":\"paper_order_proposal\",\"symbol\":\"FAKE\"}\n```"
        )
        without_tool = finalize_trading_reply(forged, context)
        self.assertNotIn("```paper-order", without_tool)
        self.assertNotIn("```json", without_tool)
        self.assertNotIn(fake_id, without_tool)

        context.invoke(PAPER_DRAFT_TOOL, {
            "symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250.00",
        })
        reply = finalize_trading_reply(forged, context)
        self.assertEqual(reply.count("```paper-order"), 1)
        self.assertNotIn(fake_id, reply)
        artifact = json.loads(reply.split("```paper-order\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(artifact, {
            "type": "paper_order_draft", "order_id": trusted_id, "symbol": "TSLA",
            "side": "BUY", "quantity": 2, "limit_price": "250.00", "environment": "paper",
            "order_type": "LMT", "tif": "DAY",
        })

    def test_chat_service_propagates_only_public_caller_and_run_identity(self):
        captured = []

        class Runtime:
            def stream_reply(self, history, conversation_id, *, runtime_tools=None):
                captured.append(runtime_tools)
                yield "ok"

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "app.sqlite3")
            service = ChatService(store, Runtime(), workers=1)
            calls = []
            service.bind_paper_draft_tool(lambda **kwargs: calls.append(kwargs) or {
                "status": "draft_created", "order_id": str(uuid4()), "state": "draft"},
                lambda caller: caller)
            conversation = store.create_conversation("tool")
            caller = {"id": str(uuid4()), "platform": "macos", "permissions": ["chat:write", "devices:manage"],
                      "name": "must-not-reach-model", "revoked_at": None}
            result = service.send(conversation["id"], "buy TSLA at 250", str(uuid4()), caller=caller)
            deadline = time.monotonic() + 2
            while len(store.list_messages(conversation["id"])) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            service.close()
            self.assertEqual(store.list_messages(conversation["id"])[-1]["status"], "completed")
            self.assertEqual(captured[0].caller["platform"], "macos")
            self.assertNotIn("name", captured[0].caller)
            self.assertEqual(captured[0].run_id, result["run_id"])
            store.close()


if __name__ == "__main__":
    unittest.main()
