---
name: wheelhouse
description: Park or end this wheelhouse session. Use only when the person runs /wheelhouse park or /wheelhouse end (or /wheelhouse:wheelhouse).
argument-hint: park | end
disable-model-invocation: true
---

The person ran `/wheelhouse $ARGUMENTS`. Act on the first word of the arguments.

**`park`**: the person is parking this session, and may come back to it next week.

1. Bring each of this session's wheelhouse items up to date with `update_item`, so the wheelhouse shows where things stand.
2. Call the `wheelhouse` MCP tool `park_session`.
3. Reply in one line: the session is parked and the tab can be closed. It comes back from the wheelhouse's Parked shelf.

**`end`**: the person is ending this session. The work is finished and the wheelhouse's data for it will be deleted.

1. If your own instructions describe anything to do when a session ends (such as saving memory), do it first: the wheelhouse keeps nothing for this session afterwards.
2. Call the `wheelhouse` MCP tool `end_session`.
3. Reply in one line: the session has ended and the tab can be closed.

**Anything else, or nothing**: do nothing to the session. Reply in one line: usage is `/wheelhouse park` (come back to it later) or `/wheelhouse end` (finished, wheelhouse data deleted).
