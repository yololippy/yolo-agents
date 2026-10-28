"""Chat orchestration: accepted-message idempotency and serialized conversations."""

from concurrent.futures import ThreadPoolExecutor
from collections import deque
import threading
from uuid import uuid4

from .agent_runtime import LoginRequiredError, RuntimeConfigurationError, RuntimeGuardrailError, RuntimeStateError
from .codex_runtime import CodexBusyError


class ChatService:
    def __init__(self, store, runtime, workers=4):
        self.store, self.runtime = store, runtime
        self.agent_id, self.app_name = 'base', 'base'
        self.lock = threading.Lock()
        self.accept_locks = {}
        self.queues, self.active = {}, set()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chat")
        self.closing = threading.Event()

    def conversation_lock(self, collection, conversation_id):
        with self.lock:
            return collection.setdefault(conversation_id, threading.Lock())

    def send(self, conversation_id, content, client_message_id):
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
                    if message["content"] != content:
                        raise ValueError("client_message_id already used with different content")
                    return {"message": message, "run_id": message["run_id"], "status": "duplicate"}
            run_id = str(uuid4())
            with self.lock:
                # Account mutations hold this lock while idle. Persisting acceptance
                # and marking the queue active must be one admission boundary.
                message = self.store.append_message(conversation_id, "user", content,
                                                    client_message_id=client_message_id, run_id=run_id)
                self.queues.setdefault(conversation_id, deque()).append(run_id)
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

    def drain(self, conversation_id):
        while True:
            with self.lock:
                pending = self.queues[conversation_id]
                if not pending:
                    self.active.discard(conversation_id)
                    del self.queues[conversation_id]
                    return
                run_id = pending.popleft()
            self.reply(conversation_id, run_id)

    def history(self, conversation_id, run_id):
        messages = self.store.list_messages(conversation_id)
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

    def reply(self, conversation_id, run_id):
        try:
            chunks, size = [], 0
            for delta in self.runtime.stream_reply(self.history(conversation_id, run_id), conversation_id):
                if not isinstance(delta, str):
                    raise TypeError("runtime delta must be text")
                size += len(delta)
                if size > 1_000_000:
                    raise ValueError("reply exceeded the configured output bound")
                chunks.append(delta)
                self.store.append_event("assistant.delta", conversation_id, {"run_id": run_id, "delta": delta})
            content = "".join(chunks)
            if not content.strip():
                raise ValueError("runtime returned an empty reply")
            self.store.finish_run(conversation_id, run_id, content)
        except RuntimeGuardrailError:
            self.fail(conversation_id, run_id, "guardrail_blocked", "内容检查已拦截本次回复。请移除消息中的密钥或令牌后重新发送；若未提供凭据，可新建会话重试。")
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
                        self.store.finish_run(conversation["id"], message["run_id"], cached)
                    else:
                        self.fail(conversation["id"], message["run_id"], "run_interrupted", "服务已重启；中断的回复未自动重试，请重新发送。")

    def close(self):
        self.closing.set()
        self.pool.shutdown(wait=True, cancel_futures=False)
