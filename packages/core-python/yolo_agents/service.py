"""Chat orchestration: accepted-message idempotency and serialized conversations."""

from concurrent.futures import ThreadPoolExecutor
from collections import deque
import threading
from uuid import uuid4

from .agent_runtime import (
    LoginRequiredError, RuntimeConfigurationError, RuntimeConnectionError,
    RuntimeGuardrailError, RuntimeProviderError, RuntimeStateError,
)
from .codex_runtime import CodexBusyError
from .media import MAX_IMAGES_PER_MESSAGE, MAX_MESSAGE_IMAGE_BYTES, RuntimeMedia
from .runtime_tools import RuntimeToolContext


class ContextResetError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class ChatService:
    def __init__(self, store, runtime, workers=4):
        self.store, self.runtime = store, runtime
        self.agent_id, self.app_name = 'base', 'base'
        self.lock = threading.Lock()
        self.accept_locks = {}
        self.queues, self.active = {}, set()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chat")
        self.closing = threading.Event()
        self._paper_draft_tool = None
        self._paper_cancel_tool = None
        self._trading_read_tool = None
        self._trading_confirmation = None
        self._paper_draft_reauthorize = None

    def bind_paper_draft_tool(self, handler, reauthorize):
        """Bind the Trading Agent's draft-only callback; never an execution API."""
        if not callable(handler) or not callable(reauthorize) or self._paper_draft_tool is not None:
            raise RuntimeError("Paper draft tool binding is invalid")
        self._paper_draft_tool = handler
        self._paper_draft_reauthorize = reauthorize

    def bind_trading_tools(self, draft_handler, read_handler, cancel_handler,
                           confirmation_handler, reauthorize):
        """Bind Trading Agent model tools; all broker reads stay application-owned."""
        if (not callable(read_handler) or not callable(cancel_handler)
                or not callable(confirmation_handler)):
            raise RuntimeError("Trading read tool binding is invalid")
        self.bind_paper_draft_tool(draft_handler, reauthorize)
        self._trading_read_tool = read_handler
        self._paper_cancel_tool = cancel_handler
        self._trading_confirmation = confirmation_handler

    def conversation_lock(self, collection, conversation_id):
        with self.lock:
            return collection.setdefault(conversation_id, threading.Lock())

    def send(self, conversation_id, content, client_message_id, *, caller=None, media_ids=()):
        if self.closing.is_set():
            raise RuntimeError("service shutting down")
        with self.conversation_lock(self.accept_locks, conversation_id):
            conversation = self.store.get_conversation(conversation_id)
            if not conversation:
                raise LookupError("conversation not found")
            if conversation["archived"]:
                raise ValueError("conversation is archived")
            for message in self.store.list_messages(conversation_id):
                if message.get("client_message_id") == client_message_id:
                    if (message["content"] != content
                            or [item["id"] for item in message.get("media", [])] != list(media_ids)):
                        raise ValueError("client_message_id already used with different content")
                    return {"message": message, "run_id": message["run_id"], "status": "duplicate"}
            run_id = str(uuid4())
            if content == "/new" and not media_ids:
                if not self.store.is_primary_conversation(conversation_id):
                    raise ContextResetError("primary_only", "/new 只用于主会话。")
                with self.lock:
                    if conversation_id in self.active:
                        raise ContextResetError(
                            "conversation_busy", "请等待当前回复完成后再使用 /new。",
                        )
                    message = self.store.reset_model_context(
                        conversation_id, client_message_id, run_id,
                        "已开启新的模型上下文。历史记录与长期记忆均已保留。",
                    )
                return {"message": message, "run_id": run_id, "status": "accepted"}
            with self.lock:
                # Account mutations hold this lock while idle. Persisting acceptance
                # and marking the queue active must be one admission boundary.
                message = self.store.append_message(conversation_id, "user", content,
                                                    client_message_id=client_message_id, run_id=run_id,
                                                    media_ids=media_ids)
                public_caller = None if caller is None else {
                    key: caller.get(key) for key in ("id", "platform", "permissions", "revoked_at")
                }
                self.queues.setdefault(conversation_id, deque()).append((run_id, public_caller))
                start = conversation_id not in self.active
                self.active.add(conversation_id)
            if start:
                self.pool.submit(self.drain, conversation_id)
            return {"message": message, "run_id": run_id, "status": "accepted"}

    def codex_account(self, action):
        name = {"read": "account_status", "login": "start_login", "logout": "logout", "cancel": "cancel_login"}[action]
        operation = getattr(self.runtime, name, None)
        if operation is None:
            raise RuntimeConfigurationError("当前模式不支持此账号操作。")
        if action == "read":
            return operation()
        with self.lock:
            if self.active or self.closing.is_set():
                raise CodexBusyError("请等待全部已接受或排队的回复完成后再更改 Codex 登录。")
            return operation()

    def update_conversation(self, conversation_id, **fields):
        with self.conversation_lock(self.accept_locks, conversation_id):
            if "model" in fields:
                with self.lock:
                    if conversation_id in self.active:
                        raise ValueError("Cannot switch model while a conversation has accepted runs")
                self.runtime.validate_model(fields["model"])
            return self.store.update_conversation(conversation_id, **fields)

    def ensure_primary_conversation(self, user_id="local-user"):
        """Bind a compatible existing conversation once, or create a new primary."""
        owner_scope = self.runtime.primary_owner_scope(user_id)
        existing = self.store.primary_conversation(owner_scope)
        if existing is not None:
            return existing
        preferred = next((
            conversation for conversation in self.store.list_conversations()
            if not self.store.is_primary_conversation(conversation["id"])
            and self.runtime.conversation_matches_primary_owner(conversation["id"], user_id)
        ), None)
        return self.store.ensure_primary_conversation(
            owner_scope, preferred["id"] if preferred is not None else None,
        )

    def drain(self, conversation_id):
        while True:
            with self.lock:
                pending = self.queues[conversation_id]
                if not pending:
                    self.active.discard(conversation_id)
                    del self.queues[conversation_id]
                    return
                run_id, caller = pending.popleft()
            self.reply(conversation_id, run_id, caller)

    def history(self, conversation_id, run_id):
        messages = self.store.list_context_messages(conversation_id)
        assistants = {m.get("run_id"): m for m in messages if m["role"] == "assistant" and m["status"] == "completed"}
        result = []
        for message in messages:
            if message["role"] != "user":
                continue
            result.append(message)
            if message.get("run_id") == run_id:
                break
            previous = assistants.get(message.get("run_id"))
            if previous:
                result.append(previous)
        return result

    def reply(self, conversation_id, run_id, caller=None):
        try:
            history = self.history(conversation_id, run_id)
            latest = history[-1]
            if self._trading_confirmation is not None and caller is not None:
                handled = self._trading_confirmation(
                    history=tuple(history), caller=dict(caller),
                    reauthorize=lambda: self._paper_draft_reauthorize(caller),
                )
                if handled is not None:
                    if not isinstance(handled, str) or not handled.strip():
                        raise ValueError("invalid trading confirmation response")
                    self.store.append_event("assistant.delta", conversation_id,
                                            {"run_id": run_id, "delta": handled})
                    self.store.finish_run(conversation_id, run_id, handled)
                    return
            runtime_tools = None
            if self._paper_draft_tool is not None and caller is not None:
                runtime_tools = RuntimeToolContext(
                    conversation_id=conversation_id,
                    user_message_id=latest["id"],
                    run_id=run_id,
                    caller=caller,
                    trusted_messages=tuple({
                        "role": message.get("role"), "content": message.get("content", ""),
                    } for message in history),
                    create_paper_draft=self._paper_draft_tool,
                    reauthorize=lambda: self._paper_draft_reauthorize(caller),
                    read_trading_data=self._trading_read_tool,
                    cancel_paper_order=self._paper_cancel_tool,
                )
            chunks, size = [], 0
            media_ids, media_size = [], 0
            stream = (self.runtime.stream_reply(history, conversation_id, runtime_tools=runtime_tools)
                      if runtime_tools is not None else self.runtime.stream_reply(history, conversation_id))
            for delta in stream:
                if isinstance(delta, str):
                    size += len(delta)
                    if size > 1_000_000:
                        raise ValueError("reply exceeded the configured output bound")
                    chunks.append(delta)
                    self.store.append_event("assistant.delta", conversation_id,
                                            {"run_id": run_id, "delta": delta})
                elif isinstance(delta, RuntimeMedia):
                    if len(media_ids) >= MAX_IMAGES_PER_MESSAGE:
                        raise ValueError("runtime returned too many images")
                    media_size += len(delta.data)
                    if media_size > MAX_MESSAGE_IMAGE_BYTES:
                        raise ValueError("runtime image bytes exceeded the configured output bound")
                    media_id = str(uuid4())
                    media, _ = self.store.create_media(
                        media_id, delta.data, delta.media_type, source="runtime", run_id=run_id,
                        filename=delta.filename, alt_text=delta.alt_text,
                    )
                    media_ids.append(media_id)
                    self.store.append_event("assistant.media", conversation_id,
                                            {"run_id": run_id, "media": media})
                else:
                    raise TypeError("runtime delta must be text or image media")
            content = "".join(chunks)
            if not content.strip() and not media_ids:
                raise ValueError("runtime returned an empty reply")
            self.store.finish_run(conversation_id, run_id, content, media_ids=media_ids)
        except RuntimeGuardrailError:
            self.fail(conversation_id, run_id, "guardrail_blocked", "内容检查已拦截本次回复。请移除消息中的密钥或令牌后重新发送；若未提供凭据，可新建会话重试。")
        except RuntimeConnectionError:
            self.fail(conversation_id, run_id, "model_connection_reset", "模型连接已自动重置；本次消息未自动重试，请重新发送。")
        except RuntimeProviderError as exc:
            # RuntimeProviderError is the only provider exception allowed to
            # cross this boundary; its message contains allowlisted metadata and
            # fixed local wording, never the provider's arbitrary response body.
            self.fail(conversation_id, run_id, "agent_failed", str(exc))
        except LoginRequiredError:
            self.fail(conversation_id, run_id, "login_required", "请先在 Mac 完成当前 Agent 的 ChatGPT 登录，再发送新消息。")
        except RuntimeStateError:
            self.fail(conversation_id, run_id, "conversation_state_invalid", "当前会话状态无法继续，请新建会话；原记录已保留，不会自动重建或批准工具。")
        except RuntimeConfigurationError:
            self.fail(conversation_id, run_id, "model_configuration_error", "模型配置不可用，请在 Mac 后端检查依赖、模型名称和凭据。")
        except Exception:
            # Provider errors may include credentials or private context.
            # Keep public failure messages fixed and never retry the run.
            self.fail(conversation_id, run_id, "agent_failed", "回复生成失败，请检查本机模型配置或稍后重试。")

    def fail(self, conversation_id, run_id, code, text):
        self.store.finish_run(conversation_id, run_id, text, status="failed", error={"code": code, "message": text})

    def recover_interrupted(self):
        """Surface unfinished accepted runs; do not replay model/tool effects."""
        for conversation in self.store.list_conversations(include_archived=True):
            messages = self.store.list_messages(conversation["id"])
            finished = {m.get("run_id") for m in messages if m["role"] == "assistant"}
            for message in messages:
                if message["role"] == "user" and message.get("run_id") and message["run_id"] not in finished:
                    cached = None
                    recover = getattr(self.runtime, "recover_completed_reply", None)
                    if recover:
                        try:
                            cached = recover(conversation["id"], message["id"])
                        except RuntimeError:
                            pass
                    if cached is not None:
                        media_ids = [item["id"] for item in self.store.media_for_run(message["run_id"])]
                        self.store.finish_run(
                            conversation["id"], message["run_id"], cached, media_ids=media_ids,
                        )
                    else:
                        self.fail(conversation["id"], message["run_id"], "run_interrupted", "服务已重启；中断的回复未自动重试，请重新发送。")

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True, cancel_futures=False)
