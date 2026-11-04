from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import base64
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from yolo_agents.store import Store
from yolo_agents.agent_runtime import DemoRuntime
from yolo_agents.service import ChatService, ContextResetError


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "app.sqlite3"
        self.store = Store(self.path)
        self.conversation = self.store.create_conversation("研究")
        self.cid = self.conversation["id"]

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_reopen_preserves_original_messages_state_and_event_cursor(self):
        message = self.store.append_message(self.cid, "user", "我关注 AAPL", "request-1")
        saved = '{"messages":[],"usage":{"requests":3},"pending":{"tool":"x"}}'
        self.store.save_agent_state(self.cid, saved)
        cursor = self.store.list_events()[-1]["seq"]
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_conversation(self.cid), self.store.list_conversations()[0])
        self.assertEqual(self.store.list_messages(self.cid), [message])
        self.assertEqual(self.store.get_agent_state(self.cid), saved)
        self.assertEqual(self.store.list_events(after=cursor), [])
        event = self.store.append_event("run.started", self.cid, {"run_id": "r1"})
        self.assertGreater(event["seq"], cursor)
        self.assertEqual(self.store.list_events(after=cursor), [event])

    def test_message_idempotency_is_scoped_to_conversation(self):
        first = self.store.append_message(self.cid, "user", "hello", "same")
        cursor = self.store.list_events()[-1]["seq"]
        self.assertEqual(self.store.append_message(self.cid, "user", "hello", "same"), first)
        self.assertEqual(self.store.list_events(after=cursor), [])
        with self.assertRaises(ValueError):
            self.store.append_message(self.cid, "user", "different", "same")
        with self.assertRaises(ValueError):
            self.store.append_message(self.cid, "assistant", "hello", "same")
        other = self.store.create_conversation("other")
        self.assertNotEqual(self.store.append_message(other["id"], "user", "hello", "same")["id"], first["id"])

    def test_message_and_event_rollback_together_on_event_write_failure(self):
        cursor = self.store.list_events()[-1]["seq"]
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""
                CREATE TRIGGER fail_message_event BEFORE INSERT ON events
                WHEN NEW.type = 'message.created'
                BEGIN SELECT RAISE(ABORT, 'simulated event write failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_message(self.cid, "user", "must rollback", "rollback-id")
        self.assertEqual(self.store.list_messages(self.cid), [])
        self.assertEqual(self.store.get_conversation(self.cid), self.conversation)
        self.assertEqual(self.store.list_events(after=cursor), [])

    def test_concurrent_duplicate_requests_have_one_message_and_one_message_event(self):
        # Include a second connection to exercise SQLite locking, not just one Python lock.
        second = Store(self.path)
        try:
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(
                    lambda n: (self.store if n % 2 else second).append_message(self.cid, "user", "same payload", "request"),
                    range(48),
                ))
            self.assertEqual(len({m["id"] for m in results}), 1)
            self.assertEqual(len(self.store.list_messages(self.cid)), 1)
            events = self.store.list_events(limit=1000)
            self.assertEqual(sum(e["type"] == "message.created" for e in events), 1)
        finally:
            second.close()

    def test_concurrent_distinct_messages_have_ordered_complete_events(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.store.append_message(self.cid, "user", f"message {n}", f"id-{n}"), range(30)))
        messages = self.store.list_messages(self.cid)
        events = self.store.list_events(limit=1000)
        created = [e["payload"]["message"] for e in events if e["type"] == "message.created"]
        self.assertEqual(messages, created)
        self.assertEqual([e["seq"] for e in events], sorted({e["seq"] for e in events}))

    def test_message_pages_are_stable_chronological_and_keep_legacy_history(self):
        messages = [
            self.store.append_message(self.cid, "user", f"message {index}", f"id-{index}")
            for index in range(7)
        ]
        first = self.store.list_messages_page(self.cid, limit=3)
        self.assertEqual(first["messages"], messages[4:])
        self.assertTrue(first["has_more"])
        second = self.store.list_messages_page(
            self.cid, before=first["next_cursor"], limit=3,
        )
        self.assertEqual(second["messages"], messages[1:4])
        self.assertTrue(second["has_more"])
        third = self.store.list_messages_page(
            self.cid, before=second["next_cursor"], limit=3,
        )
        self.assertEqual(third, {
            "messages": messages[:1], "next_cursor": None, "has_more": False,
        })
        self.assertEqual(self.store.list_messages(self.cid), messages)
        for before, limit in ((0, 3), (True, 3), (None, 0), (None, 201)):
            with self.assertRaises(ValueError):
                self.store.list_messages_page(self.cid, before=before, limit=limit)

    def test_search_is_literal_unicode_and_includes_retained_product_messages(self):
        self.store.update_conversation(self.cid, title="Straße % notes")
        self.store.append_message(self.cid, "user", "我偏好低风险投资_长期")
        for query in ("STRASSE", "%", "低风险", "_"):
            self.assertEqual([c["id"] for c in self.store.list_conversations(query)], [self.cid])
        self.assertEqual(self.store.list_conversations("not found"), [])
        self.store.save_agent_state(self.cid, '{"summary":"已压缩"}')
        self.assertEqual(self.store.list_messages(self.cid)[0]["content"], "我偏好低风险投资_长期")

    def test_archive_hides_without_deleting_and_rename_is_evented(self):
        message = self.store.append_message(self.cid, "user", "keep this")
        updated = self.store.update_conversation(self.cid, title="new", archived=True)
        self.assertTrue(updated["archived"])
        self.assertEqual(self.store.list_conversations(), [])
        self.assertEqual(self.store.list_conversations(include_archived=True), [updated])
        self.assertEqual(self.store.list_messages(self.cid), [message])
        self.assertEqual(self.store.list_events()[-1]["payload"], {"conversation": updated})
        cursor = self.store.list_events()[-1]["seq"]
        self.store.update_conversation(self.cid, title="new", archived=True)
        self.assertEqual(self.store.list_events(after=cursor), [])
        self.assertFalse(self.store.update_conversation(self.cid, archived=False)["archived"])

    def test_primary_conversation_migrates_once_and_cannot_be_archived(self):
        primary = self.store.ensure_primary_conversation("owner-a", self.cid)
        self.assertEqual(primary["id"], self.cid)
        self.assertEqual(self.store.primary_conversation("owner-a"), primary)
        self.assertTrue(self.store.is_primary_conversation(self.cid))
        self.assertEqual(
            self.store.ensure_primary_conversation("owner-a", title="ignored")["id"], self.cid,
        )
        renamed = self.store.update_conversation(self.cid, title="每天使用")
        self.assertEqual(renamed["title"], "每天使用")
        with self.assertRaises(ValueError):
            self.store.update_conversation(self.cid, archived=True)
        self.assertFalse(self.store.get_conversation(self.cid)["archived"])

    def test_primary_conversations_are_unique_per_opaque_owner(self):
        first = self.store.ensure_primary_conversation("owner-a", self.cid)
        second = self.store.ensure_primary_conversation("owner-b", self.cid)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["title"], "主会话")
        self.assertEqual(len(self.store.list_conversations()), 2)

    def test_new_resets_only_primary_model_context_and_preserves_full_history(self):
        self.store.ensure_primary_conversation("owner-a", self.cid)
        old_user = self.store.append_message(
            self.cid, "user", "old question", "old-client", run_id="old-run",
        )
        old_assistant = self.store.finish_run(self.cid, "old-run", "old answer")
        self.store.save_agent_state(self.cid, '{"conversation":{"messages":["old"]}}')

        reset = self.store.reset_model_context(
            self.cid, "reset-client", "reset-run", "context reset",
        )
        full = self.store.list_messages(self.cid)
        self.assertEqual(full[:2], [old_user, old_assistant])
        self.assertEqual([message["content"] for message in full[2:]], ["/new", "context reset"])
        self.assertEqual(reset, full[2])
        self.assertIsNone(self.store.get_agent_state(self.cid))
        self.assertEqual(self.store.list_context_messages(self.cid), [])

        cursor = self.store.list_events()[-1]["seq"]
        duplicate = self.store.reset_model_context(
            self.cid, "reset-client", "different-run", "context reset",
        )
        self.assertEqual(duplicate, reset)
        self.assertEqual(self.store.list_events(after=cursor), [])
        self.assertEqual(self.store.list_messages(self.cid), full)

        other = self.store.create_conversation("task")
        with self.assertRaises(ValueError):
            self.store.reset_model_context(
                other["id"], "task-reset", "task-run", "context reset",
            )

    def test_new_context_boundary_is_used_by_the_next_model_run(self):
        self.store.ensure_primary_conversation("owner-a", self.cid)
        self.store.append_message(
            self.cid, "user", "old question", "old-client", run_id="old-run",
        )
        self.store.finish_run(self.cid, "old-run", "old answer")
        self.store.save_agent_state(self.cid, '{"saved":true}')

        class Runtime:
            histories = []

            def stream_reply(inner, history, *_args, **_kwargs):
                inner.histories.append(history)
                yield "fresh answer"

        runtime = Runtime()
        service = ChatService(self.store, runtime, workers=1)
        try:
            reset = service.send(self.cid, "/new", "reset-client")
            self.assertEqual(reset["status"], "accepted")
            self.assertIsNone(self.store.get_agent_state(self.cid))
            service.send(self.cid, "fresh question", "fresh-client")
            deadline = time.monotonic() + 3
            while not runtime.histories and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(runtime.histories), 1)
            self.assertEqual(
                [(message["role"], message["content"]) for message in runtime.histories[0]],
                [("user", "fresh question")],
            )
        finally:
            service.close()

    def test_new_is_rejected_while_the_primary_conversation_is_running(self):
        self.store.ensure_primary_conversation("owner-a", self.cid)
        started, release = threading.Event(), threading.Event()

        class Runtime:
            def stream_reply(inner, *_args, **_kwargs):
                started.set()
                release.wait(3)
                yield "answer"

        service = ChatService(self.store, Runtime(), workers=1)
        try:
            service.send(self.cid, "question", "question-client")
            self.assertTrue(started.wait(1))
            with self.assertRaises(ContextResetError) as caught:
                service.send(self.cid, "/new", "reset-client")
            self.assertEqual(caught.exception.code, "conversation_busy")
            self.assertEqual(
                [message["content"] for message in self.store.list_messages(self.cid)],
                ["question"],
            )
        finally:
            release.set()
            service.close()

    def test_new_rolls_back_state_history_and_boundary_when_terminal_event_fails(self):
        self.store.ensure_primary_conversation("owner-a", self.cid)
        self.store.save_agent_state(self.cid, '{"saved":true}')
        before_messages = self.store.list_messages(self.cid)
        before_events = self.store.list_events()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""
                CREATE TRIGGER fail_reset_terminal BEFORE INSERT ON events
                WHEN NEW.type = 'assistant.completed'
                BEGIN SELECT RAISE(ABORT, 'simulated reset failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reset_model_context(
                self.cid, "reset-client", "reset-run", "context reset",
            )
        self.assertEqual(self.store.get_agent_state(self.cid), '{"saved":true}')
        self.assertEqual(self.store.list_messages(self.cid), before_messages)
        self.assertEqual(self.store.list_events(), before_events)
        self.assertEqual(self.store.list_context_messages(self.cid), [])

    def test_service_lazily_promotes_latest_compatible_conversation(self):
        latest = self.store.create_conversation("最近任务")
        runtime = DemoRuntime(self.store)
        service = ChatService(self.store, runtime)
        try:
            primary = service.ensure_primary_conversation()
            self.assertEqual(primary["id"], latest["id"])
            self.assertEqual(service.ensure_primary_conversation()["id"], latest["id"])
        finally:
            service.close()
            runtime.close()

    def test_event_pagination_uses_global_cursor_and_conversation_filter(self):
        other = self.store.create_conversation("other")["id"]
        first = self.store.append_event("one", self.cid, {"text": "中文"})
        self.store.append_event("two", other, {})
        third = self.store.append_event("three", self.cid, {"value": [1, True, None]})
        self.assertEqual(self.store.list_events(after=first["seq"], limit=1, conversation_id=self.cid), [third])
        self.assertEqual(self.store.list_events(limit=0), [])
        for bad in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                self.store.list_events(after=bad)

    def test_invalid_payload_or_state_does_not_replace_valid_data(self):
        self.store.save_agent_state(self.cid, '{"valid":true}')
        for state in ("not json", "[]", '{"value":NaN}'):
            with self.assertRaises(ValueError):
                self.store.save_agent_state(self.cid, state)
        self.assertEqual(self.store.get_agent_state(self.cid), '{"valid":true}')
        before = self.store.list_events()
        with self.assertRaises(ValueError):
            self.store.append_event("invalid", self.cid, {"value": float("nan")})
        self.assertEqual(self.store.list_events(), before)

    def test_missing_conversations_fail_without_orphan_records(self):
        self.assertIsNone(self.store.get_conversation("missing"))
        for operation in (
            lambda: self.store.append_message("missing", "user", "no"),
            lambda: self.store.append_event("x", "missing", {}),
            lambda: self.store.save_agent_state("missing", "{}"),
            lambda: self.store.list_messages("missing"),
            lambda: self.store.update_conversation("missing", title="new"),
        ):
            with self.assertRaises(KeyError):
                operation()

    def test_output_shapes_are_json_serializable_and_timestamps_are_utc(self):
        message = self.store.append_message(self.cid, "assistant", "response", status="completed", run_id="run-1")
        self.assertEqual(set(message), {"id", "conversation_id", "role", "content", "created_at", "status", "client_message_id", "run_id", "media"})
        self.assertEqual(message["media"], [])
        self.assertEqual(set(self.conversation), {"id", "title", "created_at", "updated_at", "archived", "model"})
        self.assertTrue(message["created_at"].endswith("Z"))
        self.assertEqual(datetime.fromisoformat(message["created_at"]).utcoffset().total_seconds(), 0)
        json.dumps([self.conversation, message, self.store.list_events()], allow_nan=False)

    def test_media_is_idempotent_private_and_atomically_attached_to_message(self):
        media_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        metadata, created = self.store.create_media(
            media_id, PNG_1X1, "image/png", source="upload", uploader_device_id="device-1",
            filename="chart.png",
        )
        self.assertTrue(created)
        self.assertEqual(metadata["width"], 1)
        self.assertEqual(metadata["height"], 1)
        self.assertNotIn("data", metadata)
        self.assertNotIn("uploader_device_id", metadata)
        self.assertEqual(self.store.create_media(
            media_id, PNG_1X1, "image/png", source="upload", uploader_device_id="device-1",
            filename="chart.png",
        ), (metadata, False))
        message = self.store.append_message(
            self.cid, "user", "", "image-message", media_ids=(media_id,),
        )
        self.assertEqual(message["media"], [metadata])
        self.assertEqual(self.store.read_media(media_id), (metadata, PNG_1X1))
        event = [item for item in self.store.list_events() if item["type"] == "message.created"][-1]
        self.assertEqual(event["payload"]["message"]["media"], [metadata])
        with self.assertRaises(ValueError):
            self.store.append_message(self.cid, "user", "reuse", media_ids=(media_id,))

    def test_runtime_media_attaches_only_to_completed_assistant_run(self):
        media_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        metadata, _ = self.store.create_media(
            media_id, PNG_1X1, "image/png", source="runtime", run_id="run-image",
            alt_text="one pixel",
        )
        message = self.store.finish_run(
            self.cid, "run-image", "generated", media_ids=(media_id,),
        )
        self.assertEqual(message["media"], [metadata])
        self.assertEqual(self.store.media_for_run("run-image"), [metadata])
        with self.assertRaises(ValueError):
            self.store.finish_run(self.cid, "failed-image", "failed", status="failed", media_ids=(media_id,))

    def test_media_store_quota_does_not_reject_an_exact_idempotent_retry(self):
        media_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        with patch("yolo_agents.store.MAX_MEDIA_STORE_BYTES", len(PNG_1X1)):
            metadata, _ = self.store.create_media(
                media_id, PNG_1X1, "image/png", source="upload", uploader_device_id="device-1",
            )
            self.assertEqual(self.store.create_media(
                media_id, PNG_1X1, "image/png", source="upload", uploader_device_id="device-1",
            ), (metadata, False))
            with self.assertRaises(RuntimeError):
                self.store.create_media(
                    "dddddddd-dddd-4ddd-8ddd-dddddddddddd", PNG_1X1, "image/png",
                    source="upload", uploader_device_id="device-1",
                )

    def test_future_schema_is_not_silently_downgraded(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version = 999")
        with self.assertRaises(RuntimeError):
            Store(self.path)

    def test_finish_run_has_one_terminal_message_and_event_and_keeps_user_same_run(self):
        self.store.append_message(self.cid, "user", "question", run_id="run-1")
        first = self.store.finish_run(self.cid, "run-1", "answer")
        cursor = self.store.list_events()[-1]["seq"]
        self.assertEqual(self.store.finish_run(self.cid, "run-1", "different", status="failed", error={"code": "late"}), first)
        self.assertEqual(self.store.list_events(after=cursor), [])
        self.assertEqual(len(self.store.list_messages(self.cid)), 2)
        final = [e for e in self.store.list_events() if e["type"] == "assistant.completed"]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["payload"], {"run_id": "run-1", "message": first})

    def test_failed_run_saves_public_error_and_is_not_overwritten_by_late_success(self):
        error = {"code": "interrupted", "message": "服务已重启"}
        failed = self.store.finish_run(self.cid, "run-1", "服务已重启", status="failed", error=error)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.store.list_events()[-1]["payload"], {"run_id": "run-1", "message": failed, "error": error})
        self.assertEqual(self.store.finish_run(self.cid, "run-1", "late answer"), failed)
        with self.assertRaises(ValueError):
            self.store.finish_run(self.cid, "run-2", "x", status="streaming")

    def test_terminal_event_failure_rolls_back_message_and_all_related_events(self):
        cursor = self.store.list_events()[-1]["seq"]
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""
                CREATE TRIGGER fail_terminal BEFORE INSERT ON events
                WHEN NEW.type = 'assistant.completed'
                BEGIN SELECT RAISE(ABORT, 'simulated terminal event failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish_run(self.cid, "run-1", "must not persist")
        self.assertEqual(self.store.list_messages(self.cid), [])
        self.assertEqual(self.store.list_events(after=cursor), [])

    def test_concurrent_finishers_commit_one_terminal_event_across_connections(self):
        second = Store(self.path)
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda n: (self.store if n % 2 else second).finish_run(self.cid, "same-run", f"answer-{n}", status="failed" if n % 2 else "completed"),
                    range(24),
                ))
            self.assertEqual(len({m["id"] for m in results}), 1)
            events = self.store.list_events()
            self.assertEqual(sum(e["type"] in ("assistant.completed", "assistant.failed") for e in events), 1)
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
