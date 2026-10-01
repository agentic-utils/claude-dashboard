"""SQLite store: the only source of truth for the board.

Every public write runs in its own transaction and is committed (and, with
synchronous=FULL in WAL mode, fsynced) before the call returns, so a crash
never loses a change the caller was told about.
"""

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path.home() / ".local/state/claude-board/board.db"

KINDS = {"task": "T", "question": "Q", "agent": "A"}
STATUSES = {
    "task": {"todo", "running", "blocked", "waiting", "done", "dropped"},
    "question": {"open", "answered", "closed"},
    "agent": {"running", "done", "failed"},
}
INITIAL_STATUS = {"task": "todo", "question": "open", "agent": "running"}
CLOSED = {"done", "dropped", "closed", "failed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    ticket       TEXT NOT NULL DEFAULT '',
    brief        TEXT NOT NULL DEFAULT '',
    cwd          TEXT NOT NULL,
    parked       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    launched_at  TEXT,
    claude_pid   INTEGER,
    claude_start INTEGER,
    boot_id      TEXT,
    heartbeat_at TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ref        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (session_id, ref)
);
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    item_ref     TEXT,
    author       TEXT NOT NULL CHECK (author IN ('claude', 'person')),
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_pending
    ON messages (session_id) WHERE delivered_at IS NULL;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    path = Path(os.environ.get("BOARD_DB") or DEFAULT_DB).expanduser()
    if str(path.resolve()).startswith("/mnt/"):
        raise ValueError(f"refusing {path}: SQLite locking on /mnt/ is unreliable, use the Linux filesystem")
    return path


class Store:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit mode: transactions are explicit, one per write
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        # one connection, possibly many threads (the MCP server runs tools in worker threads)
        self.lock = threading.RLock()

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def _one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    # sessions

    def create_session(self, cwd: str, name: str = "", ticket: str = "", brief: str = "") -> str:
        sid = str(uuid.uuid4())
        with self.tx() as db:
            db.execute(
                "INSERT INTO sessions (id, name, ticket, brief, cwd, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, name, ticket, brief, cwd, now()),
            )
        return sid

    def session(self, sid: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM sessions WHERE id = ?", (sid,))

    def sessions(self) -> list[sqlite3.Row]:
        return self._all(
            """SELECT s.*,
                 (SELECT count(*) FROM items i WHERE i.session_id = s.id
                    AND i.kind = 'question' AND i.status = 'open') AS open_questions,
                 (SELECT count(*) FROM items i WHERE i.session_id = s.id
                    AND i.status = 'running') AS running
               FROM sessions s ORDER BY s.created_at"""
        )

    def mark_launched(self, sid: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE sessions SET launched_at = ? WHERE id = ?", (now(), sid))

    def register(self, sid: str, pid: int, start: int, boot_id: str) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE sessions SET claude_pid = ?, claude_start = ?, boot_id = ?, heartbeat_at = ? WHERE id = ?",
                (pid, start, boot_id, now(), sid),
            )

    def heartbeat(self, sid: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE sessions SET heartbeat_at = ? WHERE id = ?", (now(), sid))

    def set_parked(self, sid: str, parked: bool) -> None:
        with self.tx() as db:
            db.execute("UPDATE sessions SET parked = ? WHERE id = ?", (int(parked), sid))

    def end(self, sid: str) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM sessions WHERE id = ?", (sid,))

    # items

    def post_item(self, sid: str, kind: str, title: str, body: str = "", status: str | None = None) -> str:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        status = status or INITIAL_STATUS[kind]
        self._check_status(kind, status)
        with self.tx() as db:
            n = db.execute(
                "SELECT count(*) FROM items WHERE session_id = ? AND kind = ?", (sid, kind)
            ).fetchone()[0]
            ref = f"{KINDS[kind]}{n + 1}"
            db.execute(
                """INSERT INTO items (session_id, ref, kind, title, body, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, ref, kind, title, body, status, now(), now()),
            )
        return ref

    def update_item(self, sid: str, ref: str, *, status=None, title=None, body=None, note=None) -> None:
        item = self.item(sid, ref)
        if item is None:
            raise KeyError(f"no item {ref} in this session")
        if status is not None:
            self._check_status(item["kind"], status)
        with self.tx() as db:
            db.execute(
                """UPDATE items SET status = coalesce(?, status), title = coalesce(?, title),
                   body = coalesce(?, body), updated_at = ? WHERE session_id = ? AND ref = ?""",
                (status, title, body, now(), sid, ref),
            )
            if note:
                db.execute(
                    """INSERT INTO messages (session_id, item_ref, author, body, created_at, delivered_at)
                       VALUES (?, ?, 'claude', ?, ?, ?)""",
                    (sid, ref, note, now(), now()),
                )

    def item(self, sid: str, ref: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM items WHERE session_id = ? AND ref = ?", (sid, ref))

    def items(self, sid: str | None = None, include_closed: bool = True) -> list[sqlite3.Row]:
        sql = """SELECT i.*, s.name AS session_name FROM items i JOIN sessions s ON s.id = i.session_id
                 WHERE (? IS NULL AND s.parked = 0) OR i.session_id = ?"""
        rows = self._all(sql, (sid, sid))
        if not include_closed:
            rows = [r for r in rows if r["status"] not in CLOSED]
        return sorted(rows, key=inbox_rank)

    @staticmethod
    def _check_status(kind: str, status: str) -> None:
        if status not in STATUSES[kind]:
            raise ValueError(f"{kind} status must be one of {sorted(STATUSES[kind])}")

    # messages

    def send(self, sid: str, body: str, item_ref: str | None = None) -> None:
        """A message from the person; the session's monitor delivers it."""
        with self.tx() as db:
            db.execute(
                "INSERT INTO messages (session_id, item_ref, author, body, created_at) VALUES (?, ?, 'person', ?, ?)",
                (sid, item_ref, body, now()),
            )
            db.execute(
                """UPDATE items SET status = 'answered', updated_at = ?
                   WHERE session_id = ? AND ref = ? AND kind = 'question' AND status = 'open'""",
                (now(), sid, item_ref),
            )

    def pending(self, sid: str) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM messages WHERE session_id = ? AND delivered_at IS NULL ORDER BY id", (sid,)
        )

    def take_pending(self, sid: str) -> list[sqlite3.Row]:
        """Read and mark delivered in one transaction, so the monitor and get_input()
        can never both hand Claude the same message."""
        with self.tx() as db:
            rows = db.execute(
                """UPDATE messages SET delivered_at = ? WHERE session_id = ? AND delivered_at IS NULL
                   RETURNING *""", (now(), sid)
            ).fetchall()
        return sorted(rows, key=lambda m: m["id"])   # RETURNING order is unspecified

    def thread(self, sid: str, ref: str) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM messages WHERE session_id = ? AND item_ref = ? ORDER BY id", (sid, ref)
        )


RANK = {"open": 0, "blocked": 1, "waiting": 1, "answered": 2, "running": 3, "todo": 4}


def inbox_rank(item) -> tuple:
    """Questions waiting on the person first, then blocked, running, the rest; newest first within a rank."""
    return (RANK.get(item["status"], 9), _neg_time(item["updated_at"]))


def _neg_time(iso: str) -> float:
    return -datetime.fromisoformat(iso).timestamp()
