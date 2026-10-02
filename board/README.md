# claude-board

A sidecar board for running several Claude Code sessions in parallel, on Windows with
WSL and Windows Terminal. Prototype: see `../.plan/session-board.md` for the design.

- **Inbox.** Every task, question and subagent status from every session in one list,
  open questions first. Pick one to read its full detail and thread, type an answer,
  Ctrl+S to send. The answer reaches that session as a notification, even when it's idle.
- **Sessions.** New session (directory, optional name, optional ticket, opening brief)
  opens a Windows Terminal tab running Claude. Restore brings back sessions that died
  (reboot, crash), one at a time or all at once; nothing restarts on its own. Park
  hides a session until you restore it. End deletes its board data. On a running
  session, Park and End only ask the session to do it (press again to cancel or force);
  the board never deletes anything by itself.
- **Durable.** State is in SQLite at `~/.local/state/claude-board/board.db`
  (override with `BOARD_DB`), committed to disk on every change.

Only sessions launched from the board are tracked. Nothing is installed into your
Claude Code settings: each launched session gets the board's MCP server, monitor,
protocol and `/wheelhouse park` / `/wheelhouse end` commands through its own launch flags.

## Run

```
cd board
uv sync
uv run claude-board
```

Keys: `i` inbox, `s` sessions, `n` new session, `Esc` all sessions, `Ctrl+S` send, `q` quit.

## Test

```
uv run pytest
```
