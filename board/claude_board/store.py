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
from datetime import datetime, timedelta, timezone
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
    heartbeat_at TEXT,
    end_requested_at  TEXT,
    park_requested_at TEXT,
    end_told_at  TEXT,
    park_told_at TEXT
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
    claimed_at   TEXT,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_pending
    ON messages (session_id) WHERE delivered_at IS NULL;
"""

# columns added after the first release: (table, column, type)
ADDED_COLUMNS = [("sessions", "end_requested_at", "TEXT"), ("sessions", "park_requested_at", "TEXT"),
                 ("sessions", "end_told_at", "TEXT"), ("sessions", "park_told_at", "TEXT"),
                 ("messages", "claimed_at", "TEXT")]
REQUESTS = ("end", "park")   # what the board can ask a running session to do
CLAIM_TIMEOUT = 30   # seconds before a claim from a monitor that died mid-print is retaken


class SessionGone(LookupError):
    """The session's row has gone: it ended itself, or was force-ended on the board."""

    def __init__(self, sid: str = ""):
        super().__init__(f"session {sid} no longer exists")


GONE_TEXT = ("[board] This session was force-ended on the board (or has ended): its board data "
             "is gone. Stop using board tools.")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp() -> str:
    """A precise timestamp: tells one claim or request from the next within a second."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
        # one connection, possibly many threads (the MCP server runs tools in worker threads)
        self.lock = threading.RLock()
        with self.tx() as db:   # one transaction, so processes starting together can't both migrate
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            for table, column, kind in ADDED_COLUMNS:
                if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

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

    @staticmethod
    def _require(db, sid: str) -> None:
        """Inside a write transaction (BEGIN IMMEDIATE holds the write lock), so the row
        can't vanish between this check and the write that follows."""
        if db.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is None:
            raise SessionGone(sid)

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
        """A (re)launch also drops any request left over from the last run."""
        with self.tx() as db:
            db.execute("""UPDATE sessions SET launched_at = ?, end_requested_at = NULL,
                          park_requested_at = NULL, end_told_at = NULL, park_told_at = NULL
                          WHERE id = ?""", (now(), sid))

    def register(self, sid: str, pid: int, start: int, boot_id: str) -> None:
        with self.tx() as db:
            self._register(db, sid, pid, start, boot_id)

    def register_if_free(self, sid: str, pid: int, start: int, boot_id: str, is_alive) -> bool:
        """Compare-and-set: register only if no live Claude holds the session. One
        transaction, so two tabs racing to start the same session can't both win."""
        with self.tx() as db:
            s = db.execute("SELECT claude_pid, claude_start, boot_id FROM sessions WHERE id = ?", (sid,)).fetchone()
            if s is None or is_alive(s["claude_pid"], s["claude_start"], s["boot_id"]):
                return False
            self._register(db, sid, pid, start, boot_id)
        return True

    @staticmethod
    def _register(db, sid, pid, start, boot_id) -> None:
        db.execute(
            "UPDATE sessions SET claude_pid = ?, claude_start = ?, boot_id = ?, heartbeat_at = ? WHERE id = ?",
            (pid, start, boot_id, now(), sid),
        )

    def request_end(self, sid: str) -> None:
        self.request(sid, "end")

    def request_park(self, sid: str) -> None:
        self.request(sid, "park")

    def request(self, sid: str, what: str) -> None:
        """The person pressed End or Park on a running session; its monitor passes this on.
        Nothing happens to the data until the session acts, or the person forces it."""
        assert what in REQUESTS
        with self.tx() as db:
            self._require(db, sid)
            db.execute(f"UPDATE sessions SET {what}_requested_at = coalesce({what}_requested_at, ?) "
                       "WHERE id = ?", (stamp(), sid))

    def tell_request(self, sid: str, what: str, asked: str) -> bool:
        """The monitor is about to pass a request on: record that, only if it is still the
        current request (a cancel may have just cleared it). Returns whether to print it."""
        with self.tx() as db:
            return db.execute(f"UPDATE sessions SET {what}_told_at = ? WHERE id = ? "
                              f"AND {what}_requested_at = ?", (asked, sid, asked)).rowcount == 1

    def untell_request(self, sid: str, what: str) -> None:
        """Printing the request failed: it was never passed on after all."""
        with self.tx() as db:
            db.execute(f"UPDATE sessions SET {what}_told_at = NULL WHERE id = ?", (sid,))

    def cancel_request(self, sid: str, what: str) -> None:
        """Clear a request. Only a session that was told about it hears that it's cancelled."""
        assert what in REQUESTS
        with self.tx() as db:
            self._require(db, sid)
            told = db.execute(f"SELECT {what}_told_at FROM sessions WHERE id = ?", (sid,)).fetchone()[0]
            db.execute(f"UPDATE sessions SET {what}_requested_at = NULL, {what}_told_at = NULL "
                       "WHERE id = ?", (sid,))
            if told:
                db.execute(
                    "INSERT INTO messages (session_id, author, body, created_at) VALUES (?, 'person', ?, ?)",
                    (sid, f"The person cancelled the {what} request: carry on as before.", now()),
                )

    def heartbeat(self, sid: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE sessions SET heartbeat_at = ? WHERE id = ?", (now(), sid))

    def set_parked(self, sid: str, parked: bool) -> None:
        """Parking (by the session or by force) also settles any pending park request."""
        with self.tx() as db:
            self._require(db, sid)
            db.execute("UPDATE sessions SET parked = ?, park_requested_at = NULL, park_told_at = NULL "
                       "WHERE id = ?", (int(parked), sid))

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
            self._require(db, sid)
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
        if self.session(sid) is None:
            raise SessionGone(sid)
        item = self.item(sid, ref)
        if item is None:
            raise KeyError(f"no item {ref} in this session")
        if status is not None:
            self._check_status(item["kind"], status)
        with self.tx() as db:
            self._require(db, sid)
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
            self._require(db, sid)
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

    def claim(self, sid: str, item_ref: str | None = None) -> list[sqlite3.Row]:
        """Claim undelivered messages in one transaction, so the monitor and get_input()
        never both hand Claude the same one. The claimer confirms or releases them; a claim
        left by a monitor that died mid-print is retaken after CLAIM_TIMEOUT."""
        stale = (datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT)).isoformat(timespec="microseconds")
        with self.tx() as db:
            rows = db.execute(
                """UPDATE messages SET claimed_at = ? WHERE session_id = ? AND delivered_at IS NULL
                   AND (claimed_at IS NULL OR claimed_at < ?) AND (? IS NULL OR item_ref = ?)
                   RETURNING *""", (stamp(), sid, stale, item_ref, item_ref)
            ).fetchall()
        return sorted(rows, key=lambda m: m["id"])   # RETURNING order is unspecified

    def confirm(self, claimed) -> None:
        """Mark claimed messages delivered. Only touches a message still under this claim:
        if it was retaken as stale, the new claimer confirms it."""
        self._mark(claimed, "delivered_at = ?", (now(),))

    def release(self, claimed) -> None:
        self._mark(claimed, "claimed_at = NULL", ())

    def _mark(self, claimed, assignment: str, params) -> None:
        with self.tx() as db:
            for m in claimed:
                db.execute(f"UPDATE messages SET {assignment} WHERE id = ? AND claimed_at = ?",
                           (*params, m["id"], m["claimed_at"]))

    def take_pending(self, sid: str, item_ref: str | None = None) -> list[sqlite3.Row]:
        """Claim and confirm in one go, for a caller that can't fail to show them."""
        rows = self.claim(sid, item_ref)
        self.confirm(rows)
        return rows

    def thread(self, sid: str, ref: str, in_flight: bool = True) -> list[sqlite3.Row]:
        """An item's messages. in_flight=False leaves out ones another reader has claimed
        but not yet delivered: the monitor is printing those."""
        return self._all(
            """SELECT * FROM messages WHERE session_id = ? AND item_ref = ?
               AND (? OR delivered_at IS NOT NULL OR claimed_at IS NULL) ORDER BY id""",
            (sid, ref, in_flight),
        )


RANK = {"open": 0, "blocked": 1, "waiting": 1, "answered": 2, "running": 3, "todo": 4}


def inbox_rank(item) -> tuple:
    """Questions waiting on the person first, then blocked, running, the rest; newest first within a rank."""
    return (RANK.get(item["status"], 9), _neg_time(item["updated_at"]))


def _neg_time(iso: str) -> float:
    return -datetime.fromisoformat(iso).timestamp()
