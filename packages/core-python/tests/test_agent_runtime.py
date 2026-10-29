from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib import metadata
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from yolo_agents.agent_runtime import (
    DemoRuntime, RuntimeConfigurationError, RuntimeExecutionError,
    RuntimeStateError, create_runtime,
)
from yolo_agents.store import Store


def official_sdk_available():
    try:
        return metadata.version("pydantic-ai-slim") == "2.61.0" and metadata.version("pydantic-ai-harness") == "0.61.0"
    except metadata.PackageNotFoundError:
        return False


def fake_backend():
    """Contract double, not a claim to test the uninstalled official SDK."""
    backend = SimpleNamespace(agents=[], components=[], fail_after_first=False)

    class Usage(SimpleNamespace):
        def __init__(self, **kwargs):
            super().__init__(requests=kwargs.get("requests", 0), tool_calls=kwargs.get("tool_calls", 0), total_tokens=kwargs.get("total_tokens", 0))

    @dataclass
    class Conversation:
        messages: list
        usage: Usage
        conversation_id: str
        deferred_tool_requests: object = None

    class Adapter:
        @staticmethod
        def dump_json(conversation):
            return json.dumps({
                "messages": conversation.messages, "usage": vars(conversation.usage),
                "conversation_id": conversation.conversation_id,
                "deferred_tool_requests": conversation.deferred_tool_requests,
            }).encode()

        @staticmethod
        def validate_json(raw):
            data = json.loads(raw)
            data["usage"] = Usage(**data["usage"])
            return Conversation(**data)

    class Response:
        def __init__(self, prompt, kwargs):
            previous = kwargs.get("conversation")
            self.conversation_id = previous.conversation_id if previous else kwargs["conversation_id"]
            self.usage = Usage(
                requests=(previous.usage.requests if previous else 0) + 2,
                tool_calls=(previous.usage.tool_calls if previous else 0) + 1,
                total_tokens=(previous.usage.total_tokens if previous else 0) + 100,
            )
            self.messages = (previous.messages if previous else []) + [{"role": "user", "content": prompt}, {"role": "assistant", "content": "真实适配测试回复"}]

        def stream_text(self, **kwargs):
            assert kwargs["delta"] is True
            yield "真实适配"
            if backend.fail_after_first:
                raise OSError("sensitive-provider-error api_key=SECRET")
            yield "测试回复"

        def get_output(self):
            return "真实适配测试回复"

        def all_messages(self):
            return self.messages

    class Agent:
        def __init__(self, model, **kwargs):
            self.model, self.options, self.calls = model, kwargs, []
            backend.agents.append(self)

        @contextmanager
        def run_stream_sync(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            yield Response(prompt, kwargs)

    def component(*args, **kwargs):
        value = SimpleNamespace(args=args, kwargs=kwargs)
        backend.components.append(value)
        return value

    backend.Agent = Agent
    backend.Conversation = Conversation
    backend.ConversationTypeAdapter = Adapter
    for name in ("UsageLimits", "SqliteStepStore", "SqliteMemoryStore", "StepPersistence", "ConversationSearch", "SnapshotHistorySource", "SummarizingCompaction", "Memory"):
        setattr(backend, name, component)
    return backend


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "app.sqlite3")
        self.cid = self.store.create_conversation("test")["id"]
        self.runtimes = []

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close()
        self.store.close()
        self.directory.cleanup()

    def user(self, text, conversation_id=None):
        cid = conversation_id or self.cid
        self.store.append_message(cid, "user", text)
        return self.store.list_messages(cid)

    def pydantic(self, backend):
        with patch("yolo_agents.agent_runtime._load_pydantic_backend", return_value=backend):
            runtime = create_runtime("pydantic", "openai:configured-model", self.root, self.store)
            self.runtimes.append(runtime)
            return runtime

    def test_demo_needs_no_optional_imports_and_labels_every_reply(self):
        with patch("yolo_agents.agent_runtime._load_pydantic_backend", side_effect=AssertionError("must not import")):
            runtime = create_runtime("demo", None, self.root, self.store)
            reply = "".join(runtime.stream_reply(self.user("你好"), self.cid))
        self.assertIsInstance(runtime, DemoRuntime)
        self.assertEqual(runtime.mode, "demo")
        self.assertIn("演示模式", reply)
        self.assertIn("未连接真实模型或 IBKR", reply)
        self.assertIn("你好", reply)
        self.assertIsNotNone(self.store.get_agent_state(self.cid))

    def test_demo_reopens_without_changing_product_history(self):
        history = self.user("保存消息")
        first = "".join(create_runtime("demo", None, self.root, self.store).stream_reply(history, self.cid))
        self.store.close()
        self.store = Store(self.root / "app.sqlite3")
        second = "".join(create_runtime("demo", None, self.root, self.store).stream_reply(history, self.cid))
        self.assertEqual(first, second)
        self.assertEqual(self.store.list_messages(self.cid), history)

    def test_cancelled_demo_does_not_save_a_completed_state(self):
        runtime = DemoRuntime(self.store)
        iterator = runtime.stream_reply(self.user("x" * 200), self.cid)
        self.assertIn("演示模式", next(iterator))
        iterator.close()
        self.assertIsNone(self.store.get_agent_state(self.cid))

    def test_invalid_mode_and_missing_real_model_are_errors(self):
        for mode in ("", "automatic", "demo-if-failed"):
            with self.assertRaises(RuntimeConfigurationError):
                create_runtime(mode, None, self.root, self.store)
        with self.assertRaises(RuntimeConfigurationError):
            create_runtime("pydantic", None, self.root, self.store)

    def test_missing_dependencies_never_fall_back_to_demo(self):
        from importlib.metadata import PackageNotFoundError
        with patch("yolo_agents.agent_runtime.metadata.version", side_effect=PackageNotFoundError):
            with self.assertRaisesRegex(RuntimeConfigurationError, "缺少可用依赖"):
                create_runtime("pydantic", "openai:model", self.root, self.store)

    def test_unverified_dependency_version_is_rejected(self):
        with patch("yolo_agents.agent_runtime.metadata.version", return_value="999.0"):
            with self.assertRaisesRegex(RuntimeConfigurationError, "版本"):
                create_runtime("pydantic", "openai:model", self.root, self.store)

    def test_cross_user_and_cross_conversation_history_are_rejected(self):
        runtime = DemoRuntime(self.store)
        history = self.user("private")
        "".join(runtime.stream_reply(history, self.cid, user_id="alice"))
        with self.assertRaises(RuntimeStateError):
            "".join(runtime.stream_reply(history, self.cid, user_id="bob"))
        wrong = [{**history[0], "conversation_id": "other"}]
        with self.assertRaises(ValueError):
            "".join(runtime.stream_reply(wrong, self.cid))

    def test_real_adapter_composes_official_capabilities_and_scopes_store(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        text = "".join(runtime.stream_reply(self.user("hello"), self.cid, user_id="alice/../../x"))
        self.assertEqual(text, "真实适配测试回复")
        options = backend.agents[0].options
        self.assertEqual(options["output_type"], str)
        self.assertEqual(options["retries"], 0)
        self.assertNotIn("tools", options)
        self.assertEqual(len(options["capabilities"]), 9)
        searches = [c for c in backend.components if c.kwargs.get("scope") == "all"]
        self.assertEqual(len(searches), 1)
        paths = [Path(c.kwargs["database"]) for c in backend.components if "database" in c.kwargs]
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.is_relative_to(self.root / "users") for path in paths))
        self.assertNotIn("alice", str(paths))

    def test_real_continuation_round_trips_usage_and_does_not_resend_ui_history(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        first = "".join(runtime.stream_reply(self.user("first"), self.cid))
        state = json.loads(self.store.get_agent_state(self.cid))
        self.assertEqual(state["conversation"]["usage"]["requests"], 2)
        self.store.append_message(self.cid, "assistant", first)
        second_history = self.user("second")
        # Recreate the runtime to exercise reconstruction, not only an in-memory agent.
        second = self.pydantic(backend)
        "".join(second.stream_reply(second_history, self.cid))
        kwargs = backend.agents[-1].calls[-1][1]
        self.assertIn("conversation", kwargs)
        self.assertNotIn("message_history", kwargs)
        self.assertEqual(kwargs["conversation"].usage.requests, 2)
        self.assertEqual(kwargs["usage_limits"].kwargs["request_limit"], 10)
        restored = json.loads(self.store.get_agent_state(self.cid))["conversation"]
        self.assertEqual(restored["usage"]["requests"], 4)
        self.assertEqual(len(restored["messages"]), 4)
        self.assertEqual(self.store.list_messages(self.cid), second_history)

    def test_cached_success_recovers_api_commit_window_without_model_replay(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        history = self.user("one request")
        first = "".join(runtime.stream_reply(history, self.cid))
        self.assertEqual("".join(runtime.stream_reply(history, self.cid)), first)
        self.assertEqual(len(backend.agents[0].calls), 1)

    def test_concurrent_same_input_calls_model_once(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        history = self.user("same input")
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: "".join(runtime.stream_reply(history, self.cid)), range(18)))
        self.assertEqual(len(set(replies)), 1)
        self.assertEqual(sum(len(agent.calls) for agent in backend.agents), 1)

    def test_server_run_id_is_forwarded_to_official_runtime(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        self.store.append_message(self.cid, "user", "hello", run_id="server-run-1")
        "".join(runtime.stream_reply(self.store.list_messages(self.cid), self.cid))
        self.assertEqual(backend.agents[0].calls[0][1]["run_id"], "server-run-1")

    def test_missing_official_state_does_not_rebuild_from_product_history(self):
        self.user("old user message")
        self.store.append_message(self.cid, "assistant", "old product reply")
        history = self.user("new user message")
        backend = fake_backend()
        with self.assertRaisesRegex(RuntimeStateError, "缺少官方 Agent 状态"):
            "".join(self.pydantic(backend).stream_reply(history, self.cid))
        self.assertEqual(backend.agents, [])

    def test_stream_failure_is_not_demo_and_preserves_previous_success(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        reply = "".join(runtime.stream_reply(self.user("first"), self.cid))
        previous = self.store.get_agent_state(self.cid)
        self.store.append_message(self.cid, "assistant", reply)
        backend.fail_after_first = True
        iterator = runtime.stream_reply(self.user("second"), self.cid)
        self.assertEqual(next(iterator), "真实适配")
        with self.assertRaises(RuntimeExecutionError) as error:
            next(iterator)
        self.assertNotIn("SECRET", str(error.exception))
        self.assertNotIn("sensitive-provider-error", str(error.exception))
        self.assertEqual(self.store.get_agent_state(self.cid), previous)
        self.assertEqual(len(backend.agents[0].calls), 2)

    def test_cancelled_real_stream_does_not_commit_partial_conversation(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        iterator = runtime.stream_reply(self.user("hello"), self.cid)
        next(iterator)
        iterator.close()
        self.assertIsNone(self.store.get_agent_state(self.cid))

    def test_user_stores_and_agents_are_physically_separate(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        "".join(runtime.stream_reply(self.user("alice"), self.cid, "alice"))
        other = self.store.create_conversation("bob")["id"]
        "".join(runtime.stream_reply(self.user("bob", other), other, "bob"))
        paths = [c.kwargs["database"] for c in backend.components if "database" in c.kwargs]
        self.assertEqual(len(set(paths)), 4)
        self.assertEqual(len(backend.agents), 2)

    def test_missing_mismatched_or_deferred_state_is_not_silently_discarded(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        history = self.user("first")
        "".join(runtime.stream_reply(history, self.cid))
        self.store.append_message(self.cid, "assistant", "answer")
        history = self.user("second")
        state = json.loads(self.store.get_agent_state(self.cid))
        for change in ({"conversation_id": "wrong"}, {"deferred_tool_requests": {"calls": ["pending"]}}):
            bad = {**state, "conversation": {**state["conversation"], **change}}
            self.store.save_agent_state(self.cid, json.dumps(bad))
            with self.assertRaises(RuntimeStateError):
                "".join(runtime.stream_reply(history, self.cid))
        self.store.save_agent_state(self.cid, "{}")
        with self.assertRaises(RuntimeStateError):
            "".join(runtime.stream_reply(history, self.cid))

    def test_demo_and_real_states_cannot_be_mixed(self):
        history = self.user("hello")
        "".join(DemoRuntime(self.store).stream_reply(history, self.cid))
        with self.assertRaises(RuntimeStateError):
            "".join(self.pydantic(fake_backend()).stream_reply(history, self.cid))

    def test_demo_completed_reply_recovers_after_restart_without_events_or_state_writes(self):
        history = self.user("saved before product commit")
        expected = "".join(DemoRuntime(self.store).stream_reply(history, self.cid))
        self.store.close()
        self.store = Store(self.root / "app.sqlite3")
        runtime = DemoRuntime(self.store)
        state, events = self.store.get_agent_state(self.cid), self.store.list_events()
        with patch.object(runtime, "stream_reply", side_effect=AssertionError("must not generate")):
            with patch.object(self.store, "save_agent_state", side_effect=AssertionError("must not write")):
                self.assertEqual(runtime.recover_completed_reply(self.cid, history[-1]["id"]), expected)
        self.assertEqual(self.store.get_agent_state(self.cid), state)
        self.assertEqual(self.store.list_events(), events)
        self.assertEqual(self.store.list_messages(self.cid), history)

    def test_real_completed_reply_recovery_validates_official_state_without_agent_or_tools(self):
        backend = fake_backend()
        history = self.user("one completed model call")
        expected = "".join(self.pydantic(backend).stream_reply(history, self.cid))
        runtime = self.pydantic(backend)
        state, events = self.store.get_agent_state(self.cid), self.store.list_events()
        with patch.object(runtime, "_agent", side_effect=AssertionError("must not create model or stores")):
            with patch.object(self.store, "save_agent_state", side_effect=AssertionError("must not write")):
                self.assertEqual(runtime.recover_completed_reply(self.cid, history[-1]["id"]), expected)
        self.assertEqual(len(backend.agents), 1)
        self.assertEqual(len(backend.agents[0].calls), 1)
        self.assertEqual(self.store.get_agent_state(self.cid), state)
        self.assertEqual(self.store.list_events(), events)

    def test_recovery_returns_none_for_absent_mismatched_or_incomplete_reply(self):
        runtime = DemoRuntime(self.store)
        history = self.user("hello")
        message_id = history[-1]["id"]
        self.assertIsNone(runtime.recover_completed_reply(self.cid, message_id))
        "".join(runtime.stream_reply(history, self.cid))
        original = json.loads(self.store.get_agent_state(self.cid))
        self.assertIsNone(runtime.recover_completed_reply(self.cid, "different-message-id"))
        for change in ({"last_reply": ""}, {"last_reply": " \n"}, {"last_reply": None}, {"reply_status": "streaming"}, {"reply_status": "failed"}):
            self.store.save_agent_state(self.cid, json.dumps({**original, **change}))
            self.assertIsNone(runtime.recover_completed_reply(self.cid, message_id))

    def test_recovery_rejects_corrupt_cross_user_or_other_mode_state(self):
        runtime = DemoRuntime(self.store)
        history = self.user("private")
        "".join(runtime.stream_reply(history, self.cid, user_id="alice"))
        original = self.store.get_agent_state(self.cid)
        with self.assertRaises(RuntimeStateError):
            runtime.recover_completed_reply(self.cid, history[-1]["id"], user_id="bob")
        with self.assertRaises(RuntimeStateError):
            self.pydantic(fake_backend()).recover_completed_reply(self.cid, history[-1]["id"], user_id="alice")
        for raw in ("not-json", "{}", "[]", '{"schema_version":99}'):
            # Simulate external corruption without weakening Store's JSON checks.
            with patch.object(self.store, "get_agent_state", return_value=raw):
                with self.assertRaises(RuntimeStateError):
                    runtime.recover_completed_reply(self.cid, history[-1]["id"], user_id="alice")
        self.assertEqual(self.store.get_agent_state(self.cid), original)

    def test_real_recovery_rejects_corrupt_mismatched_or_pending_official_conversation(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        history = self.user("hello")
        "".join(runtime.stream_reply(history, self.cid))
        original = json.loads(self.store.get_agent_state(self.cid))
        invalid_conversations = (
            {}, {**original["conversation"], "conversation_id": "wrong"},
            {**original["conversation"], "deferred_tool_requests": {"calls": ["pending"]}},
        )
        for conversation in invalid_conversations:
            self.store.save_agent_state(self.cid, json.dumps({**original, "conversation": conversation}))
            with self.assertRaises(RuntimeStateError):
                runtime.recover_completed_reply(self.cid, history[-1]["id"])
        self.assertEqual(len(backend.agents[0].calls), 1)

    def test_partial_stream_never_becomes_recoverable_and_legacy_completed_state_still_reads(self):
        backend = fake_backend()
        runtime = self.pydantic(backend)
        history = self.user("first")
        reply = "".join(runtime.stream_reply(history, self.cid))
        legacy = json.loads(self.store.get_agent_state(self.cid))
        legacy.pop("reply_status")
        self.store.save_agent_state(self.cid, json.dumps(legacy))
        self.assertEqual(runtime.recover_completed_reply(self.cid, history[-1]["id"]), reply)
        self.store.append_message(self.cid, "assistant", reply)
        next_history = self.user("failed next turn")
        backend.fail_after_first = True
        with self.assertRaises(RuntimeExecutionError):
            list(runtime.stream_reply(next_history, self.cid))
        self.assertIsNone(runtime.recover_completed_reply(self.cid, next_history[-1]["id"]))
        self.assertEqual(runtime.recover_completed_reply(self.cid, history[-1]["id"]), reply)

    def test_close_rejects_active_generator_and_prevents_subsequent_use(self):
        runtime = DemoRuntime(self.store)
        history = self.user("active stream")
        iterator = runtime.stream_reply(history, self.cid)
        next(iterator)
        with self.assertRaises(RuntimeStateError):
            runtime.close()
        iterator.close()
        runtime.close()
        runtime.close()
        with self.assertRaises(RuntimeStateError):
            list(runtime.stream_reply(history, self.cid))


@unittest.skipUnless(official_sdk_available(), "Pinned official SDK is not installed in this interpreter")
class OfficialSDKTests(unittest.TestCase):
    """Real SDK/Harness with official offline models; provider requests are forbidden."""

    def setUp(self):
        import asyncio
        from pydantic_ai import Agent

        self.official_agent = Agent
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.cid = self.store.create_conversation("official offline test")["id"]
        blocker = patch("pydantic_ai.models.ALLOW_MODEL_REQUESTS", False)
        blocker.start()
        self.addCleanup(blocker.stop)
        banner = patch.dict("os.environ", {"PYDANTIC_AI_NO_BANNER": "1"})
        banner.start()
        self.addCleanup(banner.stop)
        try:
            self.original_loop = asyncio.get_event_loop()
        except RuntimeError:
            self.original_loop = None
        self.addCleanup(self.close_created_loop)

    def close_created_loop(self):
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop is not self.original_loop:
            if not loop.is_closed():
                loop.close()
            asyncio.set_event_loop(self.original_loop)

    def runtime(self, model, *, compact_after=None, agent_name="base"):
        runtime = create_runtime("pydantic", "openai:offline-placeholder", self.root, self.store,
                                 agent_name=agent_name)
        self.addCleanup(runtime.close)
        runtime._backend.Agent = lambda _model, **kwargs: self.official_agent(model, **kwargs)
        if compact_after is not None:
            strategy = runtime._backend.SummarizingCompaction
            runtime._backend.SummarizingCompaction = lambda **_: strategy(max_messages=compact_after, keep_messages=2)
        return runtime

    def ask(self, runtime, text, *, conversation_id=None, user_id="local-user"):
        from uuid import uuid4

        cid = conversation_id or self.cid
        run_id = str(uuid4())
        message = self.store.append_message(cid, "user", text, run_id=run_id)
        reply = "".join(runtime.stream_reply(self.store.list_messages(cid), cid, user_id=user_id))
        self.store.finish_run(cid, run_id, reply)
        return reply, message

    def test_official_test_model_stream_state_restart_and_sqlite_snapshots(self):
        from contextlib import closing
        import sqlite3
        from pydantic_ai import ConversationTypeAdapter
        from pydantic_ai.models.test import TestModel

        runtime = self.runtime(TestModel(call_tools=[], custom_output_text="Official offline response."))
        first, message = self.ask(runtime, "first turn")
        self.assertEqual(first, "Official offline response.")
        saved = json.loads(self.store.get_agent_state(self.cid))
        conversation = ConversationTypeAdapter.validate_json(json.dumps(saved["conversation"]))
        self.assertEqual(conversation.conversation_id, self.cid)
        self.assertGreater(conversation.usage.requests, 0)
        first_requests = conversation.usage.requests
        # A new runtime must use serialized state rather than any previous Agent object.
        restored = self.runtime(TestModel(call_tools=[], custom_output_text="Second offline response."))
        self.assertEqual(restored.recover_completed_reply(self.cid, message["id"]), first)
        reply, _ = self.ask(restored, "second turn")
        self.assertEqual(reply, "Second offline response.")
        final = json.loads(self.store.get_agent_state(self.cid))
        conversation = ConversationTypeAdapter.validate_json(json.dumps(final["conversation"]))
        self.assertGreater(conversation.usage.requests, first_requests)
        databases = list((self.root / "users").glob("*/*.sqlite3"))
        self.assertEqual({path.name for path in databases}, {"agent.sqlite3", "memory.sqlite3"})
        with closing(sqlite3.connect(next(path for path in databases if path.name == "agent.sqlite3"))) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
            self.assertGreater(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)

    def test_official_memory_tools_and_cross_conversation_history_search(self):
        from collections import deque
        from pydantic_ai.messages import ToolReturnPart
        from pydantic_ai.models.function import DeltaToolCall, FunctionModel

        requests = []
        actions = deque([
            ("write_memory", {"file": "MEMORY.md", "content": "User watches AAPL for long term research."}),
            "Memory saved.",
            ("read_memory", {"file": "MEMORY.md"}),
            "Memory recalled.",
            ("search_conversation_history", {"query": "historical_seed_marker_1122"}),
            "History recalled.",
        ])

        async def stream(messages, _info):
            requests.append(list(messages))
            action = actions.popleft()
            if isinstance(action, tuple):
                yield {0: DeltaToolCall(name=action[0], json_args=json.dumps(action[1]))}
            else:
                yield action

        runtime = self.runtime(FunctionModel(stream_function=stream))
        self.ask(runtime, "historical_seed_marker_1122 AAPL research")
        second = self.store.create_conversation("read memory")["id"]
        self.ask(runtime, "Read the remembered preference", conversation_id=second)
        third = self.store.create_conversation("recall prior conversation")["id"]
        self.ask(runtime, "Find that historical research", conversation_id=third)
        self.assertFalse(actions)
        returned = [part for messages in requests for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        memory = [str(part.content) for part in returned if part.tool_name == "read_memory"]
        history = [str(part.content) for part in returned if part.tool_name == "search_conversation_history"]
        self.assertTrue(any("User watches AAPL" in text for text in memory))
        self.assertTrue(any("historical_seed_marker_1122" in text and self.cid in text for text in history))

    def test_guardrail_blocks_memory_write_before_execution(self):
        from pydantic_ai.models.function import DeltaToolCall, FunctionModel
        from yolo_agents.agent_runtime import RuntimeGuardrailError
        import sqlite3
        from contextlib import closing

        secret = 'sk-proj-' + 'A' * 32
        async def stream(messages, _info):
            yield {0: DeltaToolCall(name='write_memory', json_args=json.dumps({'file': 'MEMORY.md', 'content': secret}))}

        runtime = self.runtime(FunctionModel(stream_function=stream))
        with self.assertRaises(RuntimeGuardrailError) as error:
            self.ask(runtime, 'Please remember this preference')
        self.assertNotIn(secret, str(error.exception))
        path = next((self.root / 'users').glob('*/memory.sqlite3'))
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_files').fetchone()[0], 0)

    def test_trading_pydantic_tool_creates_only_a_draft(self):
        from collections import deque
        from pydantic_ai.models.function import DeltaToolCall, FunctionModel
        from uuid import uuid4
        from yolo_agents.runtime_tools import RuntimeToolContext

        actions = deque([
            ("create_paper_order_draft", {"symbol": "TSLA", "side": "BUY", "quantity": 2,
                                           "limit_price": "250"}),
            "Paper draft created for Mac review.",
        ])
        async def stream(messages, _info):
            action = actions.popleft()
            if isinstance(action, tuple):
                yield {0: DeltaToolCall(name=action[0], json_args=json.dumps(action[1]))}
            else:
                yield action

        calls, order_id = [], str(uuid4())
        runtime = self.runtime(FunctionModel(stream_function=stream), agent_name="trading-agent")
        context = RuntimeToolContext(
            conversation_id=self.cid, user_message_id=str(uuid4()), run_id=str(uuid4()),
            caller={"id": str(uuid4()), "platform": "macos", "permissions": ["devices:manage"]},
            trusted_messages=({"role": "user", "content": "Buy two TSLA at limit 250"},),
            create_paper_draft=lambda **kwargs: calls.append(kwargs) or {
                "status": "draft_created", "order_id": order_id, "state": "draft"},
            reauthorize=lambda: {"platform": "macos"},
        )
        self.store.append_message(self.cid, "user", "Buy two TSLA at limit 250", run_id=context.run_id)
        reply = "".join(runtime.stream_reply(self.store.list_messages(self.cid), self.cid,
                                               runtime_tools=context))
        self.assertTrue(reply.startswith("Paper draft created for Mac review.\n\n```paper-order\n"))
        self.assertEqual(reply.count("```paper-order"), 1)
        artifact = json.loads(reply.split("```paper-order\n", 1)[1].split("\n```", 1)[0])
        self.assertEqual(artifact, {
            "type": "paper_order_draft", "order_id": order_id, "symbol": "TSLA",
            "side": "BUY", "quantity": 2, "limit_price": "250", "environment": "paper",
            "order_type": "LMT", "tif": "DAY",
        })
        self.assertEqual(calls[0]["proposal"], {
            "symbol": "TSLA", "side": "BUY", "quantity": 2, "limit_price": "250"})
        self.assertFalse(actions)

    def test_guardrail_blocks_injected_existing_memory_before_model(self):
        from pydantic_ai.models.function import FunctionModel
        from yolo_agents.agent_runtime import RuntimeGuardrailError, _scope
        from pydantic_ai_harness.memory import SqliteMemoryStore

        calls = []
        async def stream(messages, _info):
            calls.append(1)
            yield 'ok'

        runtime = self.runtime(FunctionModel(stream_function=stream))
        runtime._agent(_scope('local-user'))
        path = next((self.root / 'users').glob('*/memory.sqlite3'))
        memory = SqliteMemoryStore(database=str(path))
        runtime._worker_loop().run_until_complete(memory.write(
            _scope('local-user') + '/main/MEMORY.md', 'sk-proj-' + 'B' * 32,
            expected_version=None))
        with self.assertRaises(RuntimeGuardrailError):
            self.ask(runtime, 'hello')
        self.assertEqual(calls, [])

    def test_guardrail_blocks_legacy_history_and_cached_reply(self):
        from pydantic_ai.models.test import TestModel
        from yolo_agents.agent_runtime import RuntimeGuardrailError

        runtime = self.runtime(TestModel(call_tools=[], custom_output_text='safe reply'))
        _, message = self.ask(runtime, 'first turn')
        state = json.loads(self.store.get_agent_state(self.cid))
        secret = 'sk-proj-' + 'M' * 32
        state['last_reply'] = secret
        self.store.save_agent_state(self.cid, json.dumps(state))
        with self.assertRaises(RuntimeGuardrailError):
            runtime.recover_completed_reply(self.cid, message['id'])
        state['last_reply'] = 'safe reply'
        state['conversation']['messages'][0]['parts'][0]['content'] = secret
        self.store.save_agent_state(self.cid, json.dumps(state))
        with self.assertRaises(RuntimeGuardrailError):
            self.ask(runtime, 'continue')

    def test_official_compaction_preserves_product_messages_and_searchable_originals(self):
        from pydantic_ai.messages import ModelResponse, TextPart, ToolReturnPart
        from pydantic_ai.models.function import DeltaToolCall, FunctionModel

        requests, summaries = [], []
        search_next = [False]

        def summarize(messages, _info):
            summaries.append(list(messages))
            return ModelResponse(parts=[TextPart("Earlier turns discussed a research topic.")])

        async def stream(messages, _info):
            requests.append(list(messages))
            if search_next[0]:
                search_next[0] = False
                yield {0: DeltaToolCall(name="search_conversation_history", json_args=json.dumps({"query": "pre_compaction_marker_7788"}))}
            else:
                yield "Offline final response."

        runtime = self.runtime(FunctionModel(function=summarize, stream_function=stream), compact_after=6)
        self.ask(runtime, "pre_compaction_marker_7788 original research detail")
        for n in range(5):
            self.ask(runtime, f"Follow-up discussion {n}")
        self.assertTrue(summaries, "The real summarizing capability must execute")
        search_next[0] = True
        self.ask(runtime, "Find the original detail before compaction")
        results = [str(part.content) for messages in requests for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "search_conversation_history"]
        self.assertTrue(any("pre_compaction_marker_7788" in text for text in results))
        self.assertIn("pre_compaction_marker_7788", self.store.list_messages(self.cid)[0]["content"])
        self.assertEqual(len(self.store.list_messages(self.cid)), 14)

    def test_official_stream_failure_does_not_commit_partial_state_or_retry(self):
        import asyncio
        from pydantic_ai.models.function import FunctionModel

        requests = []

        async def stream(messages, _info):
            requests.append(list(messages))
            yield "partial output"
            await asyncio.sleep(0.1)
            raise OSError("fake-secret-provider-detail")

        runtime = self.runtime(FunctionModel(stream_function=stream))
        with self.assertRaises(RuntimeExecutionError) as error:
            self.ask(runtime, "trigger an offline stream failure")
        self.assertNotIn("fake-secret-provider-detail", str(error.exception))
        self.assertEqual(len(requests), 1)
        self.assertIsNone(self.store.get_agent_state(self.cid))
        messages = self.store.list_messages(self.cid)
        self.assertEqual(len(messages), 1)
        self.assertIsNone(runtime.recover_completed_reply(self.cid, messages[0]["id"]))

    def test_official_parallel_fresh_conversations_and_worker_loop_shutdown(self):
        import asyncio
        import threading
        from pydantic_ai.models.test import TestModel

        runtime = self.runtime(TestModel(call_tools=[], custom_output_text="Parallel offline response."))
        barrier = threading.Barrier(4)
        conversations = [self.cid] + [self.store.create_conversation(f"parallel-{n}")["id"] for n in range(3)]

        def worker(cid):
            barrier.wait(timeout=10)
            reply, _ = self.ask(runtime, "new conversation", conversation_id=cid)
            return reply, asyncio.get_event_loop()

        with ThreadPoolExecutor(max_workers=4) as pool:
            completed = list(pool.map(worker, conversations))
        self.assertEqual([reply for reply, _ in completed], ["Parallel offline response."] * 4)
        loops = {loop for _, loop in completed}
        self.assertEqual(len(loops), 4)
        self.assertTrue(all(not loop.is_closed() for loop in loops))
        runtime.close()
        self.assertTrue(all(loop.is_closed() for loop in loops))
        self.assertTrue(all(len(self.store.list_messages(cid)) == 2 for cid in conversations))

    def test_runtime_shutdown_does_not_close_a_callers_existing_event_loop(self):
        import asyncio
        from pydantic_ai.models.test import TestModel

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            runtime = self.runtime(TestModel(call_tools=[], custom_output_text="Caller loop stays open."))
            self.ask(runtime, "one turn")
            runtime.close()
            self.assertFalse(loop.is_closed())
            self.assertIs(asyncio.get_event_loop(), loop)
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
            asyncio.set_event_loop(None)

    def test_official_cancelled_stream_closes_generator_and_next_conversation_runs(self):
        import asyncio
        from pydantic_ai.models.function import FunctionModel

        calls, cleanup = [], []

        async def stream(messages, _info):
            calls.append(list(messages))
            if len(calls) == 1:
                try:
                    yield "partial output"
                    await asyncio.sleep(10)
                    yield "should never finish"
                finally:
                    cleanup.append("closed")
            else:
                yield "Next conversation works."

        runtime = self.runtime(FunctionModel(stream_function=stream))
        self.store.append_message(self.cid, "user", "cancel this stream")
        iterator = runtime.stream_reply(self.store.list_messages(self.cid), self.cid)
        self.assertEqual(next(iterator), "partial output")
        iterator.close()
        self.assertEqual(cleanup, ["closed"])
        self.assertIsNone(self.store.get_agent_state(self.cid))
        other = self.store.create_conversation("after cancellation")["id"]
        reply, _ = self.ask(runtime, "new conversation", conversation_id=other)
        self.assertEqual(reply, "Next conversation works.")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
