"""Agent-private SQLite journal. No chat schema, account IDs or raw diagnostics."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class PaperOrderStore:
    SCHEMA_VERSION = 1

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS paper_schema (version INTEGER NOT NULL)")
            row = db.execute("SELECT version FROM paper_schema").fetchone()
            if row and row[0] not in (0, self.SCHEMA_VERSION):
                raise RuntimeError("Unsupported Paper order database schema")
            for statement in (
                """CREATE TABLE IF NOT EXISTS paper_orders (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, client_request_id TEXT NOT NULL UNIQUE,
                    intent TEXT NOT NULL, intent_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                    version INTEGER NOT NULL, state TEXT NOT NULL, preview TEXT, confirmation TEXT,
                    broker_order_id TEXT, filled_quantity INTEGER NOT NULL DEFAULT 0,
                    average_fill_price TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS paper_versions (
                    order_id TEXT NOT NULL REFERENCES paper_orders(id), version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(order_id, version))""",
                """CREATE TABLE IF NOT EXISTS paper_confirmations (
                    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES paper_orders(id),
                    version INTEGER NOT NULL, content_hash TEXT NOT NULL, preview_id TEXT NOT NULL,
                    device_id TEXT NOT NULL, user_id TEXT NOT NULL, user_action TEXT NOT NULL,
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(order_id, version))""",
                """CREATE TABLE IF NOT EXISTS paper_requests (
                    id TEXT PRIMARY KEY, order_id TEXT REFERENCES paper_orders(id), operation TEXT NOT NULL,
                    input_hash TEXT NOT NULL, device_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    status TEXT NOT NULL, http_status INTEGER, response TEXT, error_code TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS paper_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT REFERENCES paper_orders(id),
                    request_id TEXT, type TEXT NOT NULL, state TEXT, error_code TEXT,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL)""",
                """CREATE UNIQUE INDEX IF NOT EXISTS paper_one_active_intent ON paper_orders(user_id, intent_key)
                    WHERE state IN ('submitting','unknown','accepted','working','partial','cancel_pending')""",
            ):
                db.execute(statement)
            if row is None:
                db.execute("INSERT INTO paper_schema VALUES (?)", (self.SCHEMA_VERSION,))
            elif row[0] == 0:
                db.execute("UPDATE paper_schema SET version=?", (self.SCHEMA_VERSION,))
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def order(db, order_id, user_id):
        row = db.execute("SELECT * FROM paper_orders WHERE id=? AND user_id=?", (order_id, user_id)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def save(db, row):
        columns = [key for key in row if key != "id"]
        db.execute("UPDATE paper_orders SET " + ",".join(key + "=?" for key in columns) + " WHERE id=?",
                   [row[key] for key in columns] + [row["id"]])

    @staticmethod
    def event(db, row, kind, timestamp, request_id=None, payload=None, error_code=None):
        db.execute("INSERT INTO paper_events(order_id,request_id,type,state,error_code,payload,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", (row["id"] if row else None, request_id, kind,
                   row["state"] if row else None, error_code, encoded(payload or {}), timestamp))
