# claude-wheelhouse

> claude-wheelhouse is an independent open-source productivity tool. It is not
> affiliated with, endorsed by or supported by Anthropic. "Claude" is a trademark of
> Anthropic.

A sidecar for running several Claude Code sessions in parallel, on Windows with WSL and
Windows Terminal. Prototype: see `../.plan/wheelhouse-sessions.md` for the design.

- **Inbox.** Every task, question and subagent status from every session in one list,
  open questions first. Pick one to read its full detail and thread, type an answer,
  Ctrl+S to send. The answer reaches that session as a notification, even when it's idle.
- **Sessions.** New session (directory, optional name, optional ticket, opening brief)
  opens a Windows Terminal tab running Claude. Restore brings back sessions that died
  (reboot, crash), one at a time or all at once; nothing restarts on its own. Park
  hides a session until you restore it. End deletes its wheelhouse data. On a running
  session, Park and End only ask the session to do it (press again to cancel or force);
  the wheelhouse never deletes anything by itself.
- **Adopt.** Brings a session the wheelhouse didn't launch into the wheelhouse: pick it
  from the recent sessions in `~/.claude/projects`. If it's still running, type `/exit` in
  its tab first (the wheelhouse never kills it), then press Adopt; it reopens in a new tab
  with `claude --resume` and the wheelhouse's flags.
- **Durable.** State is in SQLite at `~/.local/state/claude-wheelhouse/wheelhouse.db`
  (override with `WHEELHOUSE_DB`), committed to disk on every change.

Only sessions launched or adopted from the wheelhouse are tracked. Nothing is installed
into your Claude Code settings: each launched session gets everything below through its
own launch flags.

## What wheelhouse puts into your sessions

Read these before you launch a session from the wheelhouse. They tell the session how to
report to the wheelhouse, and defer to your own instructions (CLAUDE.md and the like) on
how to work.

- [`protocol.md`](claude_wheelhouse/protocol.md): appended to the session's system prompt.
- [The `/wheelhouse` skill](claude_wheelhouse/plugin/skills/wheelhouse/SKILL.md):
  `/wheelhouse park` and `/wheelhouse end`.
- [The `wheelhouse` MCP server](claude_wheelhouse/mcp_server.py): the tools the session
  posts and reads through. Each tool's description is its docstring.
- [The monitor](claude_wheelhouse/monitor.py) ([plugin entry](claude_wheelhouse/plugin/monitors/monitors.json)):
  delivers your answers and the End and Park requests as `[wheelhouse]` notifications.

`claude-wheelhouse protocol` prints all of it in one go, exactly as a session receives it,
along with the launch command.

## Run

```
cd wheelhouse
uv sync
uv run claude-wheelhouse
```

Keys: `i` inbox, `s` sessions, `n` new session, `a` adopt, `Esc` all sessions, `Ctrl+S` send, `q` quit.

## Test

```
uv run pytest
```

## Licence

MIT, see [LICENSE](LICENSE).
