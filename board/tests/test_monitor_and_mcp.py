import io
import os
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from claude_board import monitor


@pytest.mark.parametrize("ref, body, expected, desc", [
    ("Q1", "yes", "[board] from the person on Q1: yes", "answer to a question"),
    (None, "a\nb", "[board] from the person (general): a ⏎ b", "general hint, newlines flattened"),
])
def test_format_message(ref, body, expected, desc):
    assert monitor.format_message({"item_ref": ref, "body": body}) == expected, desc


def test_format_message_cuts_long_bodies():
    line = monitor.format_message({"item_ref": "Q2", "body": "x" * 5000})
    assert 'get_input("Q2")' in line and len(line) < 1700


def test_poll_once_delivers_each_message_once(store, sid):
    store.send(sid, "first")
    out = io.StringIO()
    assert monitor.poll_once(store, sid, out) == 1
    assert monitor.poll_once(store, sid, out) == 0
    assert out.getvalue().count("first") == 1


def test_mcp_server_round_trip(store, sid, db_file):
    """Start the real stdio server and drive it like Claude Code would."""
    store.register(sid, 999999, 1, "boot-x")   # as `claude_board run` would, before exec
    params = StdioServerParameters(command=sys.executable, args=["-m", "claude_board", "mcp"],
                                   env=dict(os.environ, BOARD_SESSION_ID=sid, BOARD_DB=str(db_file)))

    async def drive():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as client:
            await client.initialize()
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"post_item", "update_item", "get_input", "list_items",
                    "park_session", "end_session"} <= names
            ref = (await client.call_tool("post_item", {"kind": "question", "title": "db?",
                                                         "body": "detail"})).content[0].text
            store.send(sid, "postgres", ref)
            got = (await client.call_tool("get_input", {})).content[0].text
            return ref, got

    ref, got = anyio.run(drive)
    assert ref == "Q1"
    assert got == "[Q1] postgres"
    row = store.session(sid)
    assert row["claude_pid"] == 999999, "server leaves the pid registered by run() alone (review #4)"
    assert row["heartbeat_at"], "server beats once on start"


class BrokenOut:
    def write(self, s):
        raise BrokenPipeError

    def flush(self):
        raise BrokenPipeError


def test_a_failed_print_leaves_the_message_for_redelivery(store, sid):
    """Round-2 #2: stdout closing must not lose a claimed message."""
    store.send(sid, "keep me", "Q1")
    with pytest.raises(BrokenPipeError):
        monitor.poll_once(store, sid, BrokenOut())
    out = io.StringIO()
    assert monitor.poll_once(store, sid, out) == 1
    assert "keep me" in out.getvalue()


def test_a_claim_abandoned_by_a_killed_monitor_is_retaken(store, sid):
    """A monitor killed between claim and print leaves a stale claim behind."""
    store.send(sid, "orphan")
    assert [m["body"] for m in store.claim(sid)] == ["orphan"]
    assert store.claim(sid) == [], "a fresh claim is not taken twice"
    store.db.execute("UPDATE messages SET claimed_at = '2000-01-01T00:00:00+00:00'")
    assert [m["body"] for m in store.claim(sid)] == ["orphan"]


def test_reading_a_thread_marks_its_messages_delivered(store, sid):
    """Round-2 #6: get_input(ref) shows the thread, so the monitor must not repeat it."""
    from claude_board import mcp_server
    q = store.post_item(sid, "question", "db?")
    store.send(sid, "postgres", q)
    store.send(sid, "unrelated")
    mcp_server._store, mcp_server._sid = store, sid
    assert "postgres" in mcp_server.get_input(q)
    out = io.StringIO()
    assert monitor.poll_once(store, sid, out) == 1
    assert "unrelated" in out.getvalue() and "postgres" not in out.getvalue()


def test_a_cancel_then_a_fresh_request_arrive_in_order(store, sid):
    """R5 #2: within one poll, the cancel of the old request must come before the new one."""
    store.request(sid, "end")
    monitor.poll_once(store, sid, io.StringIO())
    store.cancel_request(sid, "end")
    store.request(sid, "end")
    out = io.StringIO()
    monitor.poll_once(store, sid, out)
    text = out.getvalue()
    assert text.index("cancelled the end request") < text.index("pressed End"), text


def test_the_monitor_survives_a_locked_database(sid, monkeypatch):
    """R5 #3: one "database is locked" must not end delivery for the rest of the session."""
    import sqlite3
    calls = []

    def poll(store, sid):
        calls.append(sid)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return None if len(calls) == 3 else 0

    monkeypatch.setattr(monitor, "poll_once", poll)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    monitor.main(sid)
    assert len(calls) == 3
