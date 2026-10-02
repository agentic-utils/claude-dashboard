"""The wheelhouse MCP server, one per launched session (stdio).

It keeps a heartbeat going, which is only a 'stalled' hint; see liveness.py. The
session's Claude process was registered by `claude_wheelhouse run` before it exec'd Claude.
"""

import functools
import os
import threading
import time

from mcp.server.mcpserver import MCPServer

from .store import GONE_TEXT, SessionGone, Store

HEARTBEAT_SECONDS = 30

server = MCPServer("wheelhouse", instructions="Wheelhouse: post and update tasks, questions and "
                   "subagent statuses; read the person's answers. See the wheelhouse protocol.")
TOOLS = []   # registered tool functions, in order: `claude-wheelhouse protocol` lists them
_store: Store | None = None
_sid = ""


def wheelhouse_tool(fn):
    """Register a tool that answers plainly once the session's wheelhouse data has gone,
    instead of failing with a raw database error."""
    @functools.wraps(fn)
    def tool(*args, **kwargs):
        if _store.session(_sid) is None:
            return GONE_TEXT
        try:
            return fn(*args, **kwargs)
        except SessionGone:
            return GONE_TEXT
    TOOLS.append(fn)
    return server.tool()(tool)


@wheelhouse_tool
def post_item(kind: str, title: str, body: str = "", status: str | None = None) -> str:
    """Create a task, question or agent item in the wheelhouse. kind: task | question | agent.
    Returns the item's ref (T1, Q1, A1...). Write the full detail in body, once."""
    return _store.post_item(_sid, kind, title, body, status)


@wheelhouse_tool
def update_item(ref: str, status: str | None = None, title: str | None = None,
                body: str | None = None, note: str | None = None) -> str:
    """Change an item's status, title or body, and/or append a progress note to its thread."""
    _store.update_item(_sid, ref, status=status, title=title, body=body, note=note)
    return f"{ref} updated"


@wheelhouse_tool
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
        # leave out what the monitor has claimed and is printing right now
        lines += [f"{m['author']} @ {m['created_at']}: {m['body']}"
                  for m in s.thread(_sid, ref, in_flight=False)]
        return "\n".join(lines)
    msgs = s.take_pending(_sid)
    return "\n\n".join(f"[{m['item_ref'] or 'general'}] {m['body']}" for m in msgs) or "nothing new"


@wheelhouse_tool
def list_items(include_closed: bool = False) -> str:
    """This session's items, one per line."""
    rows = _store.items(_sid, include_closed=include_closed)
    return "\n".join(f"{r['ref']} [{r['status']}] {r['title']}" for r in rows) or "no items"


@wheelhouse_tool
def park_session() -> str:
    """Park this session: hidden from the inbox and off the restore list until resumed."""
    _store.set_parked(_sid, True)
    return "parked; the person can close this tab"


@wheelhouse_tool
def end_session() -> str:
    """End this session for good: deletes all of its wheelhouse data. If your own instructions
    describe anything to do when a session ends, do it before calling this."""
    _store.end(_sid)
    return "ended; wheelhouse data deleted; the person can close this tab"


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
    _sid = os.environ["WHEELHOUSE_SESSION_ID"]
    _store = Store()
    _store.heartbeat(_sid)
    threading.Thread(target=_heartbeat, daemon=True).start()
    server.run("stdio")
