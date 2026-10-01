"""The board's MCP server, one per launched session (stdio).

It keeps a heartbeat going, which is only a 'stalled' hint; see liveness.py. The
session's Claude process was registered by `claude_board run` before it exec'd Claude.
"""

import os
import threading
import time

from mcp.server.mcpserver import MCPServer

from .store import Store

HEARTBEAT_SECONDS = 30

server = MCPServer("board", instructions="Session board: post and update tasks, questions and "
                   "subagent statuses; read the person's answers. See the board protocol.")
_store: Store | None = None
_sid = ""


@server.tool()
def post_item(kind: str, title: str, body: str = "", status: str | None = None) -> str:
    """Create a task, question or agent item on the board. kind: task | question | agent.
    Returns the item's ref (T1, Q1, A1...). Write the full detail in body, once."""
    return _store.post_item(_sid, kind, title, body, status)


@server.tool()
def update_item(ref: str, status: str | None = None, title: str | None = None,
                body: str | None = None, note: str | None = None) -> str:
    """Change an item's status, title or body, and/or append a progress note to its thread."""
    _store.update_item(_sid, ref, status=status, title=title, body=body, note=note)
    return f"{ref} updated"


@server.tool()
def get_input(ref: str | None = None) -> str:
    """Without ref: the person's undelivered messages (marked delivered).
    With ref: that item's full body and thread (its messages count as delivered)."""
    s = _store
    if ref:
        item = s.item(_sid, ref)
        if item is None:
            return f"no item {ref}"
        s.take_pending(_sid, ref)   # shown in the thread below, so the monitor mustn't repeat them
        lines = [f"{ref} [{item['status']}] {item['title']}", item["body"], ""]
        lines += [f"{m['author']} @ {m['created_at']}: {m['body']}" for m in s.thread(_sid, ref)]
        return "\n".join(lines)
    msgs = s.take_pending(_sid)
    return "\n\n".join(f"[{m['item_ref'] or 'general'}] {m['body']}" for m in msgs) or "nothing new"


@server.tool()
def list_items(include_closed: bool = False) -> str:
    """This session's items, one per line."""
    rows = _store.items(_sid, include_closed=include_closed)
    return "\n".join(f"{r['ref']} [{r['status']}] {r['title']}" for r in rows) or "no items"


@server.tool()
def park_session() -> str:
    """Park this session: hidden from the inbox and off the restore list until resumed."""
    _store.set_parked(_sid, True)
    return "parked; the person can close this tab"


@server.tool()
def end_session() -> str:
    """End this session for good: deletes all of its board data. Do the session-end memory save first."""
    _store.end(_sid)
    return "ended; board data deleted; the person can close this tab"


def _heartbeat() -> None:
    store = Store(_store.path)   # own connection: this runs on another thread
    while True:
        time.sleep(HEARTBEAT_SECONDS)
        try:
            store.heartbeat(_sid)
        except Exception:
            pass   # a missed beat only risks a 'stalled' hint


def main() -> None:
    global _store, _sid
    _sid = os.environ["BOARD_SESSION_ID"]
    _store = Store()
    _store.heartbeat(_sid)
    threading.Thread(target=_heartbeat, daemon=True).start()
    server.run("stdio")
