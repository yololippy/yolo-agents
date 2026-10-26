"""Explicit demo runtime and optional, official PydanticAI/Harness integration.

No broker/execution tools are registered. Importing this module needs only stdlib.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from typing import Any, Iterator

from .store import Store


class RuntimeConfigurationError(RuntimeError):
    """Runtime cannot start with the requested model/dependency configuration."""


class RuntimeStateError(RuntimeError):
    """Saved state is incompatible or needs an explicit recovery decision."""


class RuntimeExecutionError(RuntimeError):
    """A real model run failed; no demo fallback or automatic replay occurred."""


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _scope(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty server-authenticated identity")
    return "user-" + hashlib.sha256(user_id.encode()).hexdigest()


def _prompt(history: list[dict], conversation_id: str) -> tuple[str, str]:
    if not isinstance(history, list) or not history:
        raise ValueError("A persisted user message is required")
    for message in history:
        if not isinstance(message, dict) or message.get("conversation_id", conversation_id) != conversation_id:
            raise ValueError("History contains a message from another conversation")
    latest = history[-1]
    if latest.get("role") != "user" or not isinstance(latest.get("content"), str) or not latest["content"].strip():
        raise ValueError("The latest history message must be a non-empty user message")
    if latest.get("status", "completed") in ("failed", "cancelled"):
        raise ValueError("Cannot run a failed or cancelled user message")
    identity = latest.get("id")
    if not isinstance(identity, str) or not identity:
        identity = hashlib.sha256(_encode(history).encode()).hexdigest()
    return latest["content"], identity


def _chunks(text: str) -> Iterator[str]:
    for offset in range(0, len(text), 32):
        yield text[offset:offset + 32]


class _Runtime:
    def __init__(self, store: Store):
        self.store = store
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._closed = False

    def _check_open(self) -> None:
        with self._guard:
            if self._closed:
                raise RuntimeStateError("Agent runtime 已关闭。")

    def _lock(self, conversation_id: str) -> threading.Lock:
        with self._guard:
            if self._closed:
                raise RuntimeStateError("Agent runtime 已关闭。")
            return self._locks.setdefault(conversation_id, threading.Lock())

    def _load(self, conversation_id: str, user_id: str) -> dict | None:
        self._check_open()
        _scope(user_id)
        raw = self.store.get_agent_state(conversation_id)
        if raw is None:
            return None
        try:
            state = json.loads(raw)
            if not isinstance(state, dict) or state.get("schema_version") != 1:
                raise ValueError
        except (ValueError, TypeError):
            raise RuntimeStateError("保存的 Agent 状态无法识别；请保留数据并检查恢复方案。") from None
        if state.get("user_id") != user_id:
            raise RuntimeStateError("此会话不属于当前用户。")
        if state.get("mode") != self.mode:
            raise RuntimeStateError("此会话来自不同运行模式，请新建会话，避免混用演示和真实模型状态。")
        return state

    def _validate_completed_state(self, state: dict, conversation_id: str) -> None:
        """Additional mode-specific validation; never invokes a model or tools."""

    def _completed_reply(self, state: dict | None, conversation_id: str, user_message_id: str) -> str | None:
        if state is None or state.get("last_user_message_id") != user_message_id:
            return None
        reply = state.get("last_reply")
        # Version-1 states written before reply_status existed were also saved only
        # after successful completion. Explicit incomplete states are never restored.
        if state.get("reply_status", "completed") != "completed" or not isinstance(reply, str) or not reply.strip():
            return None
        self._validate_completed_state(state, conversation_id)
        return reply

    def recover_completed_reply(
        self, conversation_id: str, user_message_id: str, user_id: str = "local-user",
    ) -> str | None:
        """Read a durably completed reply without running a model, tools, or writes."""
        if not isinstance(user_message_id, str) or not user_message_id:
            raise ValueError("user_message_id must be a non-empty message ID")
        with self._lock(conversation_id):
            state = self._load(conversation_id, user_id)
            return self._completed_reply(state, conversation_id, user_message_id)

    def close(self) -> None:
        """Stop using the runtime after all request workers have joined."""
        with self._guard:
            if any(lock.locked() for lock in self._locks.values()):
                raise RuntimeStateError("请先停止并等待 Agent worker，再关闭 runtime。")
            self._closed = True


class DemoRuntime(_Runtime):
    """Deterministic transport/storage demo, with no model or fabricated account data."""

    mode = "demo"
    model_name = "demo-local"

    def stream_reply(self, history: list[dict], conversation_id: str, user_id: str = "local-user") -> Iterator[str]:
        prompt, message_id = _prompt(history, conversation_id)
        with self._lock(conversation_id):
            state = self._load(conversation_id, user_id)
            if state and state.get("last_user_message_id") == message_id:
                cached = self._completed_reply(state, conversation_id, message_id)
                if cached is None:
                    raise RuntimeStateError("已保存的回复尚未完成，不会自动重放此输入。")
                yield from _chunks(cached)
                return
            turns = sum(message.get("role") == "user" for message in history)
            reply = (
                "【演示模式｜未连接真实模型或 IBKR】\n"
                f"已收到第 {turns} 轮消息：{prompt}\n"
                "当前用于验证聊天、持久化和设备同步；不会读取账户、生成真实行情或提交订单。"
            )
            yield from _chunks(reply)
            self.store.save_agent_state(conversation_id, _encode({
                "schema_version": 1, "mode": self.mode, "user_id": user_id,
                "last_user_message_id": message_id, "last_reply": reply, "reply_status": "completed",
            }))


def _load_pydantic_backend() -> SimpleNamespace:
    """Lazy import only when real mode is explicitly requested; never install packages."""
    expected = {"pydantic-ai-slim": "2.61.0", "pydantic-ai-harness": "0.61.0"}
    try:
        for package, version in expected.items():
            if metadata.version(package) != version:
                raise RuntimeConfigurationError("PydanticAI/Harness 版本与验证基线不一致，请按 packages/core-python/requirements.txt 配置。")
        from pydantic_ai import Agent, Conversation, ConversationTypeAdapter
        from pydantic_ai.usage import UsageLimits
        from pydantic_ai_harness import ConversationSearch, Memory, StepPersistence, SummarizingCompaction
        from pydantic_ai_harness.conversation_search import SnapshotHistorySource
        from pydantic_ai_harness.memory import SqliteMemoryStore
        from pydantic_ai_harness.step_persistence import SqliteStepStore
    except (ImportError, metadata.PackageNotFoundError):
        raise RuntimeConfigurationError(
            "真实模型模式缺少可用依赖。请在项目虚拟环境安装 packages/core-python/requirements.txt 中的 PydanticAI/Harness；演示模式无需这些依赖。"
        ) from None
    return SimpleNamespace(
        Agent=Agent, Conversation=Conversation, ConversationTypeAdapter=ConversationTypeAdapter,
        UsageLimits=UsageLimits, ConversationSearch=ConversationSearch, Memory=Memory,
        StepPersistence=StepPersistence, SummarizingCompaction=SummarizingCompaction,
        SnapshotHistorySource=SnapshotHistorySource, SqliteMemoryStore=SqliteMemoryStore,
        SqliteStepStore=SqliteStepStore,
    )


@dataclass(frozen=True)
class _AgentDeps:
    namespace: str


class PydanticRuntime(_Runtime):
    """Text-only agent: official memory/search/compaction, no execution/approval tools.

    Consume and close stream_reply on the same worker thread. One runtime instance
    per local service serializes a conversation; it is not a multi-process lease.
    """

    mode = "pydantic"

    def __init__(self, model: str, data_dir: str | Path, store: Store):
        super().__init__(store)
        if not isinstance(model, str) or not model.strip() or ":" not in model:
            raise RuntimeConfigurationError("真实模式需要显式的 provider:model 模型配置。")
        self.model_name = model
        self._backend = _load_pydantic_backend()
        self._data_dir = Path(data_dir).expanduser()
        self._agents: dict[tuple[str, int], Any] = {}
        self._prepared_namespaces: set[str] = set()
        self._owned_loops: set[asyncio.AbstractEventLoop] = set()

    def _worker_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self._guard:
                self._owned_loops.add(loop)
        if loop.is_running():
            raise RuntimeConfigurationError("同步 Agent 必须在不运行 asyncio 的 worker 线程中调用。")
        return loop

    def _agent(self, namespace: str) -> Any:
        # Keep provider clients on one worker's sync-stream event loop.
        key = (namespace, threading.get_ident())
        with self._guard:
            if key not in self._agents:
                backend = self._backend
                # scope='all' is safe only because this physical store is per principal.
                directory = self._data_dir / "users" / namespace
                directory.mkdir(parents=True, exist_ok=True)
                if namespace not in self._prepared_namespaces:
                    # The official store enables WAL on first use. Two fresh
                    # worker connections can race the journal-mode upgrade even
                    # with SQLite's busy timeout. Configure only the journal mode
                    # here, under the runtime guard; all schema/data stays official.
                    for name in ("agent.sqlite3", "memory.sqlite3"):
                        with closing(sqlite3.connect(directory / name, timeout=30)) as connection:
                            connection.execute("PRAGMA journal_mode=WAL")
                    self._prepared_namespaces.add(namespace)
                steps = backend.SqliteStepStore(database=str(directory / "agent.sqlite3"))
                notes = backend.SqliteMemoryStore(database=str(directory / "memory.sqlite3"))
                self._agents[key] = backend.Agent(
                    self.model_name, deps_type=_AgentDeps, output_type=str, retries=0,
                    instructions=(
                        "你是个人 IBKR 助手，当前只开放对话、记忆和历史检索。"
                        "本服务尚无账户、行情或交易执行工具；不得声称已经查询账户或执行订单。"
                        "记忆只保存用户明确表达的长期偏好与背景，不保存密钥。"
                        "订单授权、风险限额和实时账户状态不能由记忆或历史确认代替。"
                        "历史检索为词法检索，中文可能漏召回；找不到时说明不确定。"
                        "相关历史可按需检索；请区分过去记录与当前事实。"
                    ),
                    capabilities=[
                        backend.StepPersistence(store=steps, capture_frontier=True),
                        backend.ConversationSearch(backend.SnapshotHistorySource(steps), scope="all", max_matches=5, context_lines=2),
                        backend.SummarizingCompaction(max_messages=80, keep_messages=20),
                        backend.Memory(
                            notes, namespace=lambda ctx: ctx.deps.namespace,
                            max_tokens=1500, max_lines=100,
                            injection_errors="raise",
                            write_metadata={"writer": "agent"},
                        ),
                    ],
                )
            return self._agents[key]

    def close(self) -> None:
        super().close()
        with self._guard:
            loops = list(self._owned_loops)
            if any(loop.is_running() for loop in loops):
                raise RuntimeStateError("Agent 的事件循环仍在运行，请先等待 worker 结束。")
            self._owned_loops.clear()
            self._agents.clear()
        for loop in loops:
            if loop.is_closed():
                continue
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    async def drain() -> None:
                        await asyncio.gather(*pending, return_exceptions=True)
                    loop.run_until_complete(drain())
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            finally:
                loop.close()
        try:
            current = asyncio.get_event_loop()
        except RuntimeError:
            current = None
        if current in loops:
            asyncio.set_event_loop(None)

    def _conversation_from_state(self, state: dict, conversation_id: str) -> Any:
        try:
            previous = self._backend.ConversationTypeAdapter.validate_json(_encode(state["conversation"]))
        except Exception:
            raise RuntimeStateError("官方会话状态无法反序列化，请检查版本与备份；不会从客户端历史重建工具状态。") from None
        if previous.conversation_id != conversation_id:
            raise RuntimeStateError("保存的 Agent 会话标识不匹配。")
        if previous.deferred_tool_requests is not None:
            raise RuntimeStateError("此会话有待处理工具批准，当前只读聊天接口不能自动批准或丢弃它。")
        return previous

    def _validate_completed_state(self, state: dict, conversation_id: str) -> None:
        self._conversation_from_state(state, conversation_id)

    def stream_reply(self, history: list[dict], conversation_id: str, user_id: str = "local-user") -> Iterator[str]:
        prompt, message_id = _prompt(history, conversation_id)
        namespace = _scope(user_id)
        with self._lock(conversation_id):
            state = self._load(conversation_id, user_id)
            if state and state.get("last_user_message_id") == message_id:
                cached = self._completed_reply(state, conversation_id, message_id)
                if cached is None:
                    raise RuntimeStateError("已保存的回复尚未完成，不会自动重放此输入。")
                yield from _chunks(cached)
                return
            backend = self._backend
            previous = None
            if state is not None:
                previous = self._conversation_from_state(state, conversation_id)
            elif len(history) != 1:
                raise RuntimeStateError("此会话已有产品历史但缺少官方 Agent 状态，请新建真实模型会话。")
            try:
                self._worker_loop()
                agent = self._agent(namespace)
            except RuntimeConfigurationError:
                raise
            except Exception:
                raise RuntimeConfigurationError("真实模型初始化失败，请检查已安装的供应商依赖、模型名称和服务端密钥配置。") from None
            kwargs: dict[str, Any] = {
                "deps": _AgentDeps(namespace), "timeout": 90,
                "model_settings": {"max_tokens": 2048},
                # Conversation usage is cumulative; these are per-turn increments.
                "usage_limits": backend.UsageLimits(
                    request_limit=(previous.usage.requests if previous else 0) + 8,
                    tool_calls_limit=(previous.usage.tool_calls if previous else 0) + 24,
                    total_tokens_limit=(previous.usage.total_tokens if previous else 0) + 60000,
                ),
            }
            if previous is None:
                kwargs["conversation_id"] = conversation_id
            else:
                kwargs["conversation"] = previous
            if history[-1].get("run_id"):
                kwargs["run_id"] = history[-1]["run_id"]
            pieces: list[str] = []
            try:
                with agent.run_stream_sync(prompt, **kwargs) as response:
                    deltas = response.stream_text(delta=True, debounce_by=0.05)
                    try:
                        for delta in deltas:
                            if not isinstance(delta, str):
                                raise TypeError("Expected a text delta")
                            if delta:
                                pieces.append(delta)
                                yield delta
                    except GeneratorExit:
                        # The pinned SDK's async lifecycle cannot safely receive
                        # GeneratorExit from a synchronous consumer. Close the pump
                        # and exit normally, without accepting or saving an output.
                        return
                    finally:
                        # Close the sync pump before exiting its owning run context,
                        # including when a consumer closes this outer generator.
                        close_stream = getattr(deltas, "close", None)
                        if close_stream is not None:
                            close_stream()
                    # Ensure final validation completes before the new state is accepted.
                    output = response.get_output()
                    if output != "".join(pieces):
                        raise RuntimeError("Final output disagrees with streamed text")
                    # Sync streaming exposes these public fields rather than result.conversation.
                    # This runtime has text output and no deferred tools, so pending requests are None.
                    conversation = backend.Conversation(
                        messages=response.all_messages(), usage=response.usage,
                        conversation_id=response.conversation_id, deferred_tool_requests=None,
                    )
                    encoded = backend.ConversationTypeAdapter.dump_json(conversation)
            except Exception as exc:
                raise RuntimeExecutionError(
                    f"真实模型运行失败（{type(exc).__name__}），本次未自动重试或切换到演示模式。"
                ) from None
            # No partial/failed state replaces the last successful official Conversation.
            # Cache the reply to recover the window before the API commits its assistant row.
            self.store.save_agent_state(conversation_id, _encode({
                "schema_version": 1, "mode": self.mode, "user_id": user_id,
                "model_name": self.model_name, "conversation": json.loads(encoded),
                "last_user_message_id": message_id, "last_reply": output, "reply_status": "completed",
            }))


def create_runtime(mode: str, model: str | None, data_dir: str | Path, store: Store) -> DemoRuntime | PydanticRuntime:
    if mode == "demo":
        return DemoRuntime(store)
    if mode == "pydantic":
        return PydanticRuntime(model, data_dir, store)
    raise RuntimeConfigurationError("运行模式必须明确为 demo 或 pydantic。")
