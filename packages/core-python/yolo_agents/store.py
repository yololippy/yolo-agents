"""Thread-safe product storage; model conversation state is deliberately separate."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator
from uuid import uuid4

UNSET = object()


def validate_model_id(model: str | None) -> None:
    if model is not None and (not isinstance(model, str) or not model.strip()
                              or len(model) > 200 or any(ord(c) < 32 for c in model)):
        raise ValueError("model must be null or a non-empty model identifier of at most 200 characters")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: str, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{name} must be a {'string' if allow_empty else 'non-empty string'}")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Store:
    """One shared connection guarded by a lock, with SQLite transactions across stores.

    Every product mutation and its corresponding events commit together. Call close()
    only after request/runtime workers have stopped. A database belongs to one local
    service; the runtime/API must authorize callers and serialize a conversation's runs.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(path).expanduser())
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.create_function("casefold", 1, str.casefold, deterministic=True)
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA busy_timeout = 10000")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._initialize()
        except BaseException:
            self._db.close()
            raise

    def _initialize(self) -> None:
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError("Unsupported product database schema; do not downgrade it")
            db.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
                )
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(conversations)")}
            if "model" not in columns:
                db.execute("ALTER TABLE conversations ADD COLUMN model TEXT")
            db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL, client_message_id TEXT, run_id TEXT,
                    UNIQUE (conversation_id, client_message_id)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                    conversation_id TEXT REFERENCES conversations(id),
                    payload TEXT NOT NULL, created_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS agent_states (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
                    state_json TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, seq)")
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS assistant_final_run
                ON messages(conversation_id, run_id)
                WHERE role = 'assistant' AND run_id IS NOT NULL
                AND status IN ('completed', 'failed')
            """)
            db.execute("CREATE INDEX IF NOT EXISTS events_conversation ON events(conversation_id, seq)")
            db.execute("PRAGMA user_version = 2")

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("Store is closed")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._check_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    @staticmethod
    def _conversation(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["archived"] = bool(result["archived"])
        return result

    @staticmethod
    def _message(row: sqlite3.Row) -> dict:
        result = dict(row)
        result.pop("seq", None)
        return result

    @staticmethod
    def _require_conversation(db: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
        _text(conversation_id, "conversation_id")
        row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return row

    @staticmethod
    def _insert_event(db: sqlite3.Connection, event_type: str, conversation_id: str | None, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        encoded = _json(payload)
        timestamp = _now()
        cursor = db.execute(
            "INSERT INTO events(type, conversation_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (event_type, conversation_id, encoded, timestamp),
        )
        return {
            "seq": cursor.lastrowid, "type": event_type, "conversation_id": conversation_id,
            "payload": json.loads(encoded), "created_at": timestamp,
        }

    def create_conversation(self, title: str = "新会话", model: str | None = None) -> dict:
        _text(title, "title")
        validate_model_id(model)
        timestamp = _now()
        with self._transaction() as db:
            conversation_id = str(uuid4())
            db.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at, model) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, title, timestamp, timestamp, model),
            )
            result = self._conversation(self._require_conversation(db, conversation_id))
            self._insert_event(db, "conversation.updated", conversation_id, {"conversation": result})
            return result

    def list_conversations(self, query: str = "", include_archived: bool = False) -> list[dict]:
        _text(query, "query", allow_empty=True)
        with self._lock:
            self._check_open()
            rows = self._db.execute("""
                SELECT c.* FROM conversations c
                WHERE (? OR c.archived = 0) AND (
                    ? = '' OR instr(casefold(c.title), casefold(?)) > 0 OR EXISTS (
                        SELECT 1 FROM messages m WHERE m.conversation_id = c.id
                        AND instr(casefold(m.content), casefold(?)) > 0
                    )
                ) ORDER BY c.updated_at DESC, c.id
            """, (bool(include_archived), query, query, query)).fetchall()
            return [self._conversation(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict | None:
        _text(conversation_id, "conversation_id")
        with self._lock:
            self._check_open()
            row = self._db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            return self._conversation(row) if row is not None else None

    def update_conversation(self, conversation_id: str, title: str | None = None,
                            archived: bool | None = None, model=UNSET) -> dict:
        if title is not None:
            _text(title, "title")
        if archived is not None and not isinstance(archived, bool):
            raise ValueError("archived must be a boolean")
        if model is not UNSET:
            validate_model_id(model)
        with self._transaction() as db:
            old = self._conversation(self._require_conversation(db, conversation_id))
            new_title = old["title"] if title is None else title
            new_archived = old["archived"] if archived is None else archived
            new_model = old["model"] if model is UNSET else model
            if (new_title, new_archived, new_model) == (old["title"], old["archived"], old["model"]):
                return old
            db.execute(
                "UPDATE conversations SET title = ?, archived = ?, model = ?, updated_at = ? WHERE id = ?",
                (new_title, int(new_archived), new_model, _now(), conversation_id),
            )
            result = self._conversation(self._require_conversation(db, conversation_id))
            self._insert_event(db, "conversation.updated", conversation_id, {"conversation": result})
            return result

    def append_message(
        self, conversation_id: str, role: str, content: str,
        client_message_id: str | None = None, status: str = "completed", run_id: str | None = None,
    ) -> dict:
        if role not in ("user", "assistant", "system", "tool"):
            raise ValueError("Unsupported message role")
        _text(content, "content", allow_empty=True)
        _text(status, "status")
        for name, value in (("client_message_id", client_message_id), ("run_id", run_id)):
            if value is not None:
                _text(value, name)
        with self._transaction() as db:
            self._require_conversation(db, conversation_id)
            if client_message_id is not None:
                existing = db.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? AND client_message_id = ?",
                    (conversation_id, client_message_id),
                ).fetchone()
                if existing is not None:
                    if (existing["role"], existing["content"]) != (role, content):
                        raise ValueError("client_message_id already identifies different message content")
                    return self._message(existing)
            return self._insert_message(db, conversation_id, role, content, client_message_id, status, run_id)

    def _insert_message(
        self, db: sqlite3.Connection, conversation_id: str, role: str, content: str,
        client_message_id: str | None, status: str, run_id: str | None,
    ) -> dict:
        timestamp = _now()
        message_id = str(uuid4())
        db.execute("""
            INSERT INTO messages(id, conversation_id, role, content, created_at, status, client_message_id, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (message_id, conversation_id, role, content, timestamp, status, client_message_id, run_id))
        db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id))
        result = self._message(db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())
        self._insert_event(db, "message.created", conversation_id, {"message": result})
        conversation = self._conversation(self._require_conversation(db, conversation_id))
        self._insert_event(db, "conversation.updated", conversation_id, {"conversation": conversation})
        return result

    def finish_run(
        self, conversation_id: str, run_id: str, content: str,
        status: str = "completed", error: Any = None,
    ) -> dict:
        """Commit one terminal assistant message and its terminal event atomically.

        First terminal result wins, including a concurrent completed/failed race.
        The caller supplies a public-safe error object, never raw provider errors.
        """
        _text(run_id, "run_id")
        _text(content, "content", allow_empty=True)
        if status not in ("completed", "failed"):
            raise ValueError("A run may finish only as completed or failed")
        with self._transaction() as db:
            self._require_conversation(db, conversation_id)
            existing = db.execute("""
                SELECT * FROM messages WHERE conversation_id = ? AND run_id = ?
                AND role = 'assistant' AND status IN ('completed', 'failed')
            """, (conversation_id, run_id)).fetchone()
            if existing is not None:
                return self._message(existing)
            result = self._insert_message(db, conversation_id, "assistant", content, None, status, run_id)
            payload = {"run_id": run_id, "message": result}
            if status == "failed":
                payload["error"] = error
            self._insert_event(db, f"assistant.{status}", conversation_id, payload)
            return result

    def list_messages(self, conversation_id: str) -> list[dict]:
        with self._lock:
            self._check_open()
            self._require_conversation(self._db, conversation_id)
            rows = self._db.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq", (conversation_id,))
            return [self._message(row) for row in rows]

    def append_event(self, event_type: str, conversation_id: str | None, payload: dict) -> dict:
        _text(event_type, "event_type")
        with self._transaction() as db:
            if conversation_id is not None:
                self._require_conversation(db, conversation_id)
            return self._insert_event(db, event_type, conversation_id, payload)

    def list_events(self, after: int = 0, limit: int = 200, conversation_id: str | None = None) -> list[dict]:
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 0:
            raise ValueError("after and limit must be non-negative integers")
        with self._lock:
            self._check_open()
            sql = "SELECT * FROM events WHERE seq > ?"
            params: list[Any] = [after]
            if conversation_id is not None:
                self._require_conversation(self._db, conversation_id)
                sql += " AND conversation_id = ?"
                params.append(conversation_id)
            rows = self._db.execute(sql + " ORDER BY seq LIMIT ?", (*params, limit)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def get_agent_state(self, conversation_id: str) -> str | None:
        with self._lock:
            self._check_open()
            self._require_conversation(self._db, conversation_id)
            row = self._db.execute("SELECT state_json FROM agent_states WHERE conversation_id = ?", (conversation_id,)).fetchone()
            return None if row is None else row["state_json"]

    def save_agent_state(self, conversation_id: str, state_json: str) -> None:
        _text(state_json, "state_json")
        try:
            state = json.loads(state_json, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, TypeError) as exc:
            raise ValueError("state_json must be valid JSON") from exc
        if not isinstance(state, dict):
            raise ValueError("state_json must contain a JSON object")
        with self._transaction() as db:
            self._require_conversation(db, conversation_id)
            db.execute("""
                INSERT INTO agent_states(conversation_id, state_json) VALUES (?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET state_json = excluded.state_json
            """, (conversation_id, state_json))

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
