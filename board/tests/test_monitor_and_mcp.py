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
    assert store.session(sid)["claude_pid"], "server registered the session's process"
