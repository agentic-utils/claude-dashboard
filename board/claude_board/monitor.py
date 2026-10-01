"""Plugin monitor: runs for the whole session and prints each new message from the
person as one line. Claude Code delivers each printed line to Claude as a notification.
"""

import os
import sys
import time

from .store import Store

POLL_SECONDS = 2
INLINE_LIMIT = 1500


def format_message(m) -> str:
    body = " ⏎ ".join(m["body"].splitlines())
    where = f"on {m['item_ref']}" if m["item_ref"] else "(general)"
    if len(body) > INLINE_LIMIT:
        body = body[:INLINE_LIMIT] + f" … [cut short: call get_input(\"{m['item_ref']}\") for the rest]"
    return f"[board] from the person {where}: {body}"


def poll_once(store: Store, sid: str, out=sys.stdout) -> int:
    msgs = store.pending(sid)
    for m in msgs:
        print(format_message(m), file=out, flush=True)
    store.mark_delivered([m["id"] for m in msgs])
    return len(msgs)


def main(sid: str | None = None) -> None:
    sid = sid or os.environ["BOARD_SESSION_ID"]
    store = Store()
    while store.session(sid) is not None:
        poll_once(store, sid)
        time.sleep(POLL_SECONDS)
