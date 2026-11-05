"""Thread-safe product storage; model conversation state is deliberately separate."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator
from uuid import UUID, uuid4

from .media import (
    MAX_IMAGES_PER_MESSAGE, MAX_MEDIA_STORE_BYTES, MAX_MESSAGE_IMAGE_BYTES,
    MediaStorageFullError, inspect_image, public_media, validate_filename,
)
from .media_storage import MediaStorage, TemporaryMediaStorage

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

    def __init__(self, path: str | Path, *, media_root: str | Path | None = None):
        self.path = str(path)
        self._temporary_media = None
        if self.path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(path).expanduser())
            root = Path(media_root).expanduser() if media_root is not None else Path(self.path).parent / "media"
            self._media_storage = MediaStorage(root)
        elif media_root is not None:
            self._media_storage = MediaStorage(media_root)
        else:
            self._temporary_media = TemporaryMediaStorage()
            self._media_storage = self._temporary_media.storage
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
            self._cleanup_storage_residue()
        except BaseException:
            self._db.close()
            if self._temporary_media is not None:
                self._temporary_media.cleanup()
            raise

    def _initialize(self) -> None:
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6):
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
            db.execute("""
                CREATE TABLE IF NOT EXISTS primary_conversations (
                    owner_scope TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL UNIQUE REFERENCES conversations(id),
                    created_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_context_boundaries (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
                    start_after_seq INTEGER NOT NULL CHECK (start_after_seq >= 0),
                    updated_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind = 'image'),
                    media_type TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK (byte_count > 0),
                    sha256 TEXT NOT NULL, width INTEGER NOT NULL CHECK (width > 0),
                    height INTEGER NOT NULL CHECK (height > 0), filename TEXT, alt_text TEXT,
                    created_at TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('upload', 'runtime')),
                    uploader_device_id TEXT, run_id TEXT,
                    message_id TEXT REFERENCES messages(id), position INTEGER,
                    storage_path TEXT NOT NULL,
                    UNIQUE (message_id, position),
                    CHECK ((message_id IS NULL AND position IS NULL) OR
                           (message_id IS NOT NULL AND position IS NOT NULL AND position >= 0))
                )
            """)
            media_columns = {row[1] for row in db.execute("PRAGMA table_info(media)")}
            if "data" in media_columns:
                self._migrate_media_v5(db)
            elif "storage_path" not in media_columns:
                raise RuntimeError("Unsupported media table schema; refusing unsafe recovery")
            for row in db.execute(
                "SELECT storage_path, created_at, sha256, media_type FROM media"
            ):
                self._media_storage.validate_storage_path(
                    row["storage_path"], created_at=row["created_at"],
                    digest=row["sha256"], media_type=row["media_type"],
                )
            db.execute("CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, seq)")
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS assistant_final_run
                ON messages(conversation_id, run_id)
                WHERE role = 'assistant' AND run_id IS NOT NULL
                AND status IN ('completed', 'failed')
            """)
            db.execute("CREATE INDEX IF NOT EXISTS events_conversation ON events(conversation_id, seq)")
            db.execute("CREATE INDEX IF NOT EXISTS media_message ON media(message_id, position)")
            db.execute("CREATE INDEX IF NOT EXISTS media_run ON media(run_id, created_at)")
            db.execute("PRAGMA user_version = 6")

    @staticmethod
    def _create_media_v6_table(db: sqlite3.Connection, name: str) -> None:
        if name != "media_v6":
            raise ValueError("invalid migration table")
        db.execute("""
            CREATE TABLE media_v6 (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind = 'image'),
                media_type TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK (byte_count > 0),
                sha256 TEXT NOT NULL, width INTEGER NOT NULL CHECK (width > 0),
                height INTEGER NOT NULL CHECK (height > 0), filename TEXT, alt_text TEXT,
                created_at TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('upload', 'runtime')),
                uploader_device_id TEXT, run_id TEXT,
                message_id TEXT REFERENCES messages(id), position INTEGER,
                storage_path TEXT NOT NULL,
                UNIQUE (message_id, position),
                CHECK ((message_id IS NULL AND position IS NULL) OR
                       (message_id IS NOT NULL AND position IS NOT NULL AND position >= 0))
            )
        """)

    def _migrate_media_v5(self, db: sqlite3.Connection) -> None:
        """Persist and verify every BLOB before atomically replacing the v5 table."""
        rows = db.execute("SELECT * FROM media ORDER BY id").fetchall()
        migrated = []
        for row in rows:
            data = bytes(row["data"])
            if len(data) != row["byte_count"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise RuntimeError("Legacy media BLOB failed integrity verification")
            try:
                _, media_type, width, height = inspect_image(data, row["media_type"])
            except ValueError:
                raise RuntimeError("Legacy media BLOB is not a valid declared image") from None
            if (media_type, width, height) != (row["media_type"], row["width"], row["height"]):
                raise RuntimeError("Legacy media metadata does not match its image BLOB")
            storage_path = self._media_storage.storage_path(
                row["created_at"], row["sha256"], row["media_type"],
            )
            self._media_storage.write(
                data, storage_path=storage_path, created_at=row["created_at"],
                digest=row["sha256"], media_type=row["media_type"],
            )
            # Re-read the durable target before any SQLite schema mutation.
            self._media_storage.read(
                storage_path, created_at=row["created_at"], digest=row["sha256"],
                media_type=row["media_type"], byte_count=row["byte_count"],
            )
            migrated.append((row, storage_path))

        self._create_media_v6_table(db, "media_v6")
        for row, storage_path in migrated:
            db.execute("""
                INSERT INTO media_v6(
                    id, kind, media_type, byte_count, sha256, width, height, filename,
                    alt_text, created_at, source, uploader_device_id, run_id,
                    message_id, position, storage_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["id"], row["kind"], row["media_type"], row["byte_count"], row["sha256"],
                row["width"], row["height"], row["filename"], row["alt_text"],
                row["created_at"], row["source"], row["uploader_device_id"], row["run_id"],
                row["message_id"], row["position"], storage_path,
            ))
        db.execute("DROP TABLE media")
        db.execute("ALTER TABLE media_v6 RENAME TO media")

    def _cleanup_storage_residue(self) -> None:
        with self._transaction() as db:
            referenced = {row[0] for row in db.execute("SELECT DISTINCT storage_path FROM media")}
            self._media_storage.cleanup_residue(referenced)

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
    def _media(row: sqlite3.Row) -> dict:
        return public_media(dict(row))

    @classmethod
    def _message(cls, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        result = dict(row)
        result.pop("seq", None)
        media = db.execute(
            "SELECT * FROM media WHERE message_id = ? ORDER BY position", (result["id"],)
        ).fetchall()
        result["media"] = [cls._media(item) for item in media]
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

    def ensure_primary_conversation(
        self, owner_scope: str, preferred_conversation_id: str | None = None,
        *, title: str = "主会话", model: str | None = None,
    ) -> dict:
        """Return one durable, unarchived primary conversation for an opaque owner scope.

        ``preferred_conversation_id`` supports a lossless migration of an existing
        compatible conversation. The unique mapping prevents one conversation from
        becoming the primary conversation of two authorized accounts.
        """
        _text(owner_scope, "owner_scope")
        _text(title, "title")
        validate_model_id(model)
        if preferred_conversation_id is not None:
            _text(preferred_conversation_id, "preferred_conversation_id")
        with self._transaction() as db:
            existing = db.execute(
                "SELECT conversation_id FROM primary_conversations WHERE owner_scope = ?",
                (owner_scope,),
            ).fetchone()
            if existing is not None:
                return self._conversation(self._require_conversation(db, existing["conversation_id"]))

            candidate = None
            if preferred_conversation_id is not None:
                candidate = db.execute(
                    """SELECT c.* FROM conversations c
                       WHERE c.id = ? AND c.archived = 0 AND NOT EXISTS (
                           SELECT 1 FROM primary_conversations p WHERE p.conversation_id = c.id
                       )""",
                    (preferred_conversation_id,),
                ).fetchone()
            if candidate is None:
                timestamp = _now()
                conversation_id = str(uuid4())
                db.execute(
                    "INSERT INTO conversations(id, title, created_at, updated_at, model) VALUES (?, ?, ?, ?, ?)",
                    (conversation_id, title, timestamp, timestamp, model),
                )
                candidate = self._require_conversation(db, conversation_id)
                result = self._conversation(candidate)
                self._insert_event(db, "conversation.updated", conversation_id, {"conversation": result})
            result = self._conversation(candidate)
            db.execute(
                "INSERT INTO primary_conversations(owner_scope, conversation_id, created_at) VALUES (?, ?, ?)",
                (owner_scope, result["id"], _now()),
            )
            return result

    def primary_conversation(self, owner_scope: str) -> dict | None:
        _text(owner_scope, "owner_scope")
        with self._lock:
            self._check_open()
            row = self._db.execute(
                """SELECT c.* FROM primary_conversations p
                   JOIN conversations c ON c.id = p.conversation_id
                   WHERE p.owner_scope = ?""",
                (owner_scope,),
            ).fetchone()
            return self._conversation(row) if row is not None else None

    def is_primary_conversation(self, conversation_id: str) -> bool:
        _text(conversation_id, "conversation_id")
        with self._lock:
            self._check_open()
            return self._db.execute(
                "SELECT 1 FROM primary_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone() is not None

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
            if new_archived and not old["archived"] and db.execute(
                "SELECT 1 FROM primary_conversations WHERE conversation_id = ?", (conversation_id,)
            ).fetchone() is not None:
                raise ValueError("primary conversation cannot be archived")
            if (new_title, new_archived, new_model) == (old["title"], old["archived"], old["model"]):
                return old
            db.execute(
                "UPDATE conversations SET title = ?, archived = ?, model = ?, updated_at = ? WHERE id = ?",
                (new_title, int(new_archived), new_model, _now(), conversation_id),
            )
            result = self._conversation(self._require_conversation(db, conversation_id))
            self._insert_event(db, "conversation.updated", conversation_id, {"conversation": result})
            return result

    def create_media(
        self, media_id: str, data: bytes, media_type: str, *, source: str,
        uploader_device_id: str | None = None, run_id: str | None = None,
        filename: str | None = None, alt_text: str | None = None,
    ) -> tuple[dict, bool]:
        try:
            media_id = str(UUID(media_id))
        except (AttributeError, TypeError, ValueError):
            raise ValueError("media_id must be a UUID") from None
        data, media_type, width, height = inspect_image(data, media_type)
        filename = validate_filename(filename)
        if source not in ("upload", "runtime"):
            raise ValueError("unsupported media source")
        if source == "upload":
            _text(uploader_device_id, "uploader_device_id")
            if run_id is not None or alt_text is not None:
                raise ValueError("uploaded media cannot set runtime fields")
        else:
            _text(run_id, "run_id")
            if uploader_device_id is not None:
                raise ValueError("runtime media cannot set an uploader")
            if (alt_text is not None and (not isinstance(alt_text, str) or len(alt_text) > 2000
                    or any(ord(character) < 32 and character not in "\n\t" for character in alt_text))):
                raise ValueError("invalid alt_text")
        digest = hashlib.sha256(data).hexdigest()
        timestamp = _now()
        # Preserve exact idempotency even when the record is old enough for TTL
        # cleanup or the physical quota is otherwise full.
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
            if existing is not None:
                if (existing["source"], existing["media_type"], existing["byte_count"],
                        existing["sha256"], existing["filename"], existing["alt_text"],
                        existing["uploader_device_id"], existing["run_id"]) != (
                        source, media_type, len(data), digest, filename, alt_text,
                        uploader_device_id, run_id):
                    raise ValueError("media_id already identifies different content")
                self._media_storage.read(
                    existing["storage_path"], created_at=existing["created_at"],
                    digest=existing["sha256"], media_type=existing["media_type"],
                    byte_count=existing["byte_count"],
                )
                return self._media(existing), False

        self.cleanup_orphan_media(limit=100)
        storage_path = self._media_storage.storage_path(timestamp, digest, media_type)
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
            if existing is not None:
                if (existing["source"], existing["media_type"], existing["byte_count"],
                        existing["sha256"], existing["filename"], existing["alt_text"],
                        existing["uploader_device_id"], existing["run_id"]) != (
                        source, media_type, len(data), digest, filename, alt_text,
                        uploader_device_id, run_id):
                    raise ValueError("media_id already identifies different content")
                self._media_storage.read(
                    existing["storage_path"], created_at=existing["created_at"],
                    digest=existing["sha256"], media_type=existing["media_type"],
                    byte_count=existing["byte_count"],
                )
                return self._media(existing), False
            used = db.execute("""
                SELECT COALESCE(SUM(byte_count), 0) FROM (
                    SELECT storage_path, MAX(byte_count) AS byte_count
                    FROM media GROUP BY storage_path
                )
            """).fetchone()[0]
            already_stored = db.execute(
                "SELECT 1 FROM media WHERE storage_path = ? LIMIT 1", (storage_path,),
            ).fetchone() is not None
            if used + (0 if already_stored else len(data)) > MAX_MEDIA_STORE_BYTES:
                raise MediaStorageFullError("media store quota exceeded")
            self._media_storage.write(
                data, storage_path=storage_path, created_at=timestamp,
                digest=digest, media_type=media_type,
            )
            db.execute("""
                INSERT INTO media(
                    id, kind, media_type, byte_count, sha256, width, height, filename,
                    alt_text, created_at, source, uploader_device_id, run_id, storage_path
                ) VALUES (?, 'image', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                media_id, media_type, len(data), digest, width, height, filename, alt_text,
                timestamp, source, uploader_device_id, run_id, storage_path,
            ))
            row = db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
            return self._media(row), True

    def read_media(self, media_id: str) -> tuple[dict, bytes]:
        try:
            media_id = str(UUID(media_id))
        except (AttributeError, TypeError, ValueError):
            raise ValueError("media_id must be a UUID") from None
        with self._lock:
            self._check_open()
            row = self._db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
            if row is None:
                raise KeyError(media_id)
            data = self._media_storage.read(
                row["storage_path"], created_at=row["created_at"], digest=row["sha256"],
                media_type=row["media_type"], byte_count=row["byte_count"],
            )
            return self._media(row), data

    def cleanup_orphan_media(self, *, now: datetime | None = None, limit: int = 100) -> int:
        """Delete bounded, unattached media metadata older than 24 hours, then files."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("media cleanup limit must be between 1 and 1000")
        current = datetime.now(UTC) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError("media cleanup time must be timezone-aware")
        cutoff = (current.astimezone(UTC) - timedelta(hours=24)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        with self._transaction() as db:
            rows = db.execute("""
                SELECT id, storage_path FROM media
                WHERE message_id IS NULL AND source IN ('upload', 'runtime') AND created_at < ?
                ORDER BY created_at, id LIMIT ?
            """, (cutoff, limit)).fetchall()
            if rows:
                db.executemany("DELETE FROM media WHERE id = ?", ((row["id"],) for row in rows))

        for storage_path in {row["storage_path"] for row in rows}:
            # A second immediate transaction serializes the final reference check
            # with new media inserts. The metadata deletion is already committed.
            with self._transaction() as db:
                referenced = db.execute(
                    "SELECT 1 FROM media WHERE storage_path = ? LIMIT 1", (storage_path,),
                ).fetchone()
                if referenced is None:
                    self._media_storage.remove(storage_path)
        return len(rows)

    def media_for_run(self, run_id: str) -> list[dict]:
        _text(run_id, "run_id")
        with self._lock:
            self._check_open()
            rows = self._db.execute(
                "SELECT * FROM media WHERE source = 'runtime' AND run_id = ? ORDER BY created_at, id",
                (run_id,),
            ).fetchall()
            return [self._media(row) for row in rows]

    def append_message(
        self, conversation_id: str, role: str, content: str,
        client_message_id: str | None = None, status: str = "completed", run_id: str | None = None,
        media_ids: tuple[str, ...] | list[str] = (),
    ) -> dict:
        if role not in ("user", "assistant", "system", "tool"):
            raise ValueError("Unsupported message role")
        _text(content, "content", allow_empty=True)
        _text(status, "status")
        for name, value in (("client_message_id", client_message_id), ("run_id", run_id)):
            if value is not None:
                _text(value, name)
        media_ids = self._validate_media_ids(media_ids)
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
                    result = self._message(db, existing)
                    if [item["id"] for item in result["media"]] != list(media_ids):
                        raise ValueError("client_message_id already identifies different media")
                    return result
            return self._insert_message(
                db, conversation_id, role, content, client_message_id, status, run_id, media_ids,
            )

    @staticmethod
    def _validate_media_ids(media_ids: object) -> tuple[str, ...]:
        if (not isinstance(media_ids, (tuple, list)) or len(media_ids) > MAX_IMAGES_PER_MESSAGE
                or len(set(media_ids)) != len(media_ids)):
            raise ValueError("media IDs are invalid")
        result = []
        for value in media_ids:
            try:
                normalized = str(UUID(value))
            except (AttributeError, TypeError, ValueError):
                raise ValueError("media IDs are invalid") from None
            result.append(normalized)
        return tuple(result)

    @staticmethod
    def _attach_media(
        db: sqlite3.Connection, message_id: str, media_ids: tuple[str, ...], source: str,
    ) -> None:
        total = 0
        for position, media_id in enumerate(media_ids):
            row = db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
            if row is None or row["source"] != source or row["message_id"] is not None:
                raise ValueError("media is unavailable or already attached")
            total += row["byte_count"]
            if total > MAX_MESSAGE_IMAGE_BYTES:
                raise ValueError("message image bytes exceed the supported range")
            db.execute(
                "UPDATE media SET message_id = ?, position = ? WHERE id = ? AND message_id IS NULL",
                (message_id, position, media_id),
            )

    def _insert_message(
        self, db: sqlite3.Connection, conversation_id: str, role: str, content: str,
        client_message_id: str | None, status: str, run_id: str | None,
        media_ids: tuple[str, ...] = (),
    ) -> dict:
        timestamp = _now()
        message_id = str(uuid4())
        db.execute("""
            INSERT INTO messages(id, conversation_id, role, content, created_at, status, client_message_id, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (message_id, conversation_id, role, content, timestamp, status, client_message_id, run_id))
        if media_ids:
            self._attach_media(
                db, message_id, media_ids, "upload" if role == "user" else "runtime",
            )
        db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (timestamp, conversation_id))
        result = self._message(db, db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone())
        self._insert_event(db, "message.created", conversation_id, {"message": result})
        conversation = self._conversation(self._require_conversation(db, conversation_id))
        self._insert_event(db, "conversation.updated", conversation_id, {"conversation": conversation})
        return result

    def finish_run(
        self, conversation_id: str, run_id: str, content: str,
        status: str = "completed", error: Any = None,
        media_ids: tuple[str, ...] | list[str] = (),
    ) -> dict:
        """Commit one terminal assistant message and its terminal event atomically.

        First terminal result wins, including a concurrent completed/failed race.
        The caller supplies a public-safe error object, never raw provider errors.
        """
        _text(run_id, "run_id")
        _text(content, "content", allow_empty=True)
        if status not in ("completed", "failed"):
            raise ValueError("A run may finish only as completed or failed")
        media_ids = self._validate_media_ids(media_ids)
        if status == "failed" and media_ids:
            raise ValueError("failed messages cannot attach runtime media")
        with self._transaction() as db:
            self._require_conversation(db, conversation_id)
            existing = db.execute("""
                SELECT * FROM messages WHERE conversation_id = ? AND run_id = ?
                AND role = 'assistant' AND status IN ('completed', 'failed')
            """, (conversation_id, run_id)).fetchone()
            if existing is not None:
                return self._message(db, existing)
            result = self._insert_message(
                db, conversation_id, "assistant", content, None, status, run_id, media_ids,
            )
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
            return [self._message(self._db, row) for row in rows]

    def list_messages_page(
        self, conversation_id: str, *, before: int | None = None, limit: int = 50,
    ) -> dict:
        """Return one stable newest-first cursor page in chronological display order."""
        if before is not None and (type(before) is not int or before <= 0):
            raise ValueError("before must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 through 200")
        with self._lock:
            self._check_open()
            self._require_conversation(self._db, conversation_id)
            sql = "SELECT * FROM messages WHERE conversation_id = ?"
            params: list[Any] = [conversation_id]
            if before is not None:
                sql += " AND seq < ?"
                params.append(before)
            rows = self._db.execute(
                sql + " ORDER BY seq DESC LIMIT ?", (*params, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            visible = rows[:limit]
            next_cursor = visible[-1]["seq"] if has_more and visible else None
            return {
                "messages": [self._message(self._db, row) for row in reversed(visible)],
                "next_cursor": next_cursor,
                "has_more": has_more,
            }

    def list_context_messages(self, conversation_id: str) -> list[dict]:
        """Return only product messages in the current model-context epoch."""
        with self._lock:
            self._check_open()
            self._require_conversation(self._db, conversation_id)
            rows = self._db.execute("""
                SELECT * FROM messages
                WHERE conversation_id = ? AND seq > COALESCE((
                    SELECT start_after_seq FROM conversation_context_boundaries
                    WHERE conversation_id = ?
                ), 0)
                ORDER BY seq
            """, (conversation_id, conversation_id)).fetchall()
            return [self._message(self._db, row) for row in rows]

    def reset_model_context(
        self, conversation_id: str, client_message_id: str, run_id: str,
        response_text: str,
    ) -> dict:
        """Atomically mark a fresh model epoch without deleting product history."""
        _text(client_message_id, "client_message_id")
        _text(run_id, "run_id")
        _text(response_text, "response_text")
        with self._transaction() as db:
            conversation = self._require_conversation(db, conversation_id)
            if conversation["archived"]:
                raise ValueError("conversation is archived")
            if db.execute(
                "SELECT 1 FROM primary_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone() is None:
                raise ValueError("only a primary conversation can reset model context")
            existing = db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND client_message_id = ?",
                (conversation_id, client_message_id),
            ).fetchone()
            if existing is not None:
                if (existing["role"], existing["content"]) != ("user", "/new"):
                    raise ValueError("client_message_id already identifies different message content")
                return self._message(db, existing)

            user = self._insert_message(
                db, conversation_id, "user", "/new", client_message_id, "completed", run_id,
            )
            db.execute("DELETE FROM agent_states WHERE conversation_id = ?", (conversation_id,))
            assistant = self._insert_message(
                db, conversation_id, "assistant", response_text, None, "completed", run_id,
            )
            self._insert_event(db, "assistant.completed", conversation_id, {
                "run_id": run_id, "message": assistant,
            })
            boundary = db.execute(
                "SELECT seq FROM messages WHERE id = ?", (assistant["id"],),
            ).fetchone()["seq"]
            db.execute("""
                INSERT INTO conversation_context_boundaries(conversation_id, start_after_seq, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    start_after_seq = excluded.start_after_seq,
                    updated_at = excluded.updated_at
            """, (conversation_id, boundary, _now()))
            return user

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
                if self._temporary_media is not None:
                    self._temporary_media.cleanup()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
