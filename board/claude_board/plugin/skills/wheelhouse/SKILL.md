---
name: wheelhouse
description: Park or end this board session. Use only when the person runs /wheelhouse park or /wheelhouse end (or /board:wheelhouse).
argument-hint: park | end
disable-model-invocation: true
---

The person ran `/wheelhouse $ARGUMENTS`. Act on the first word of the arguments.

**`park`**: the person is parking this session, and may come back to it next week.

1. Bring each board item up to date with `update_item`, so the board shows where things stand.
2. Call the `board` MCP tool `park_session`.
3. Reply in one line: the session is parked and the tab can be closed. It comes back from the board's Parked shelf.

**`end`**: the person is ending this session. The work is finished and the board's data for it will be deleted.

1. Do your usual session-end memory save first, as your instructions describe. Anything worth keeping must be saved there, because the board keeps nothing after this.
2. Call the `board` MCP tool `end_session`.
3. Reply in one line: the session has ended and the tab can be closed.

**Anything else, or nothing**: do nothing to the session. Reply in one line: usage is `/wheelhouse park` (come back to it later) or `/wheelhouse end` (finished, board data deleted).
