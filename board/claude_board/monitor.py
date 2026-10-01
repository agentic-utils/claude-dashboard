"""Plugin monitor: runs for the whole session and prints each new message from the
person as one line. Claude Code delivers each printed line to Claude as a notification.
"""

import os
import sys
import time
from dataclasses import dataclass, field

from .store import Store

POLL_SECONDS = 2
INLINE_LIMIT = 1500


def format_message(m) -> str:
    body = " ⏎ ".join(m["body"].splitlines())
    where = f"on {m['item_ref']}" if m["item_ref"] else "(general)"
    if len(body) > INLINE_LIMIT:
        body = body[:INLINE_LIMIT] + f" … [cut short: call get_input(\"{m['item_ref']}\") for the rest]"
    return f"[board] from the person {where}: {body}"


REQUEST_TEXT = {
    "end": "[board] The person pressed End on the board. Do the session-end memory save, "
           "then call end_session.",
    "park": "[board] The person pressed Park on the board. Bring your board items up to date, "
            "then call park_session.",
}


@dataclass
class State:
    told: dict = field(default_factory=dict)   # request -> the request stamp already passed on


def poll_once(store: Store, sid: str, out=sys.stdout, state: State | None = None) -> int:
    """Pass on new End or Park requests, then the person's messages: claim, print and flush,
    then confirm. If printing fails (stdout closed as the session dies), release the claim
    so the messages are delivered next time."""
    state = state or State()
    session = store.session(sid)
    for what, text in REQUEST_TEXT.items():
        asked = session[f"{what}_requested_at"] if session is not None else None
        if asked and state.told.get(what) != asked:
            print(text, file=out, flush=True)
        state.told[what] = asked
    msgs = store.claim(sid)
    shown = 0
    try:
        for m in msgs:
            print(format_message(m), file=out, flush=True)
            shown += 1
    finally:
        store.confirm(msgs[:shown])
        store.release(msgs[shown:])
    return shown


def main(sid: str | None = None) -> None:
    sid = sid or os.environ["BOARD_SESSION_ID"]
    store = Store()
    state = State()
    while store.session(sid) is not None:
        poll_once(store, sid, state=state)
        time.sleep(POLL_SECONDS)
