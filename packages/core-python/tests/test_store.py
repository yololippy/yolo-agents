from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yolo_agents.store import Store


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
        self.assertEqual(set(message), {"id", "conversation_id", "role", "content", "created_at", "status", "client_message_id", "run_id"})
        self.assertEqual(set(self.conversation), {"id", "title", "created_at", "updated_at", "archived", "model"})
        self.assertTrue(message["created_at"].endswith("Z"))
        self.assertEqual(datetime.fromisoformat(message["created_at"]).utcoffset().total_seconds(), 0)
        json.dumps([self.conversation, message, self.store.list_events()], allow_nan=False)

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
