# Session board

Issue: #55

## Intent

Run several Claude Code sessions in parallel and keep track of them from one place.
Every task, question and subagent status lives in a store with a stable id and a full
detail body, so Claude writes it once and refers to it by id (`Q3`), and the person
reads it in one inbox instead of scrolling back through several terminals. Answers and
hints typed into the board reach the right session, and wake it if it is idle.

The board is a sidecar: conversation still happens in the Claude Code terminal. It is
also standalone: it only tracks sessions it launched, needs no global hooks, settings or
`CLAUDE.md` changes, and leaves every other session alone.

## Shape

```
  claude-board (Textual TUI)            one per Linux user
        |  reads / writes
        v
  ~/.local/state/claude-board/board.db  SQLite, WAL, synchronous=FULL
        ^                     ^
        | MCP tools           | polls every 2 s
  board MCP server      board monitor            both started per session by the
  (per session)         (per session)            board plugin / launch flags
        \                     /
         Claude Code session in a Windows Terminal tab
```

- **Store.** SQLite is the only source of truth. Every write is its own committed
  transaction before the call returns. The TUI, MCP servers and monitors are separate
  processes that only talk through the database, so any of them can die without the
  others noticing. The TUI is a window onto the data, nothing more.
- **Location.** `~/.local/state/claude-board/board.db`, overridable with `BOARD_DB`.
  Paths under `/mnt/` are refused: SQLite locking on the Windows drive mount is not
  reliable.
- **One instance per Linux user.** Each user has their own database in their own home.

## Lifecycle

| State | Meaning | How it is derived |
|---|---|---|
| starting | launched, not yet registered | no process recorded for this launch, launched under 90 s ago |
| live | session process running | recorded Claude pid exists in `/proc` with the same start time, same boot id |
| stalled | live, but quiet | live, heartbeat older than 120 s, and the board has not just woken from sleep. A hint only |
| dead | process gone | anything else |
| parking / ending | Park or End pressed on a running session | `park_requested_at` or `end_requested_at` set and the session is live, stalled or starting |
| (ended) | `/end`, or End on a dead session, or Force end | rows deleted |

**Parked is a flag, not a state.** It is shown alongside the state ("dead · parked"),
hides the session from the inbox and keeps it off Restore All. It never hides whether
the process is running: a parked session that is still running is live, and Restore
and End treat it as live.

- **Life and death come from the process, not the heartbeat.** Sleep and hibernate keep
  the process, so the session stays live and the heartbeat resumes on wake. A reboot or
  WSL shutdown changes the boot id, so the session is dead. Start time guards against pid
  reuse.
- **Clock jumps.** The TUI compares wall-clock and monotonic time between ticks. A jump
  over 30 s means the machine slept, and the stalled hint is suppressed for 60 s while
  everything catches up.
- **Never respawn automatically.** The Sessions page has Restore on each dead row and
  Restore All. Both re-check the process immediately before launching and refuse a
  session that is still starting (launched under 90 s ago, not yet registered), so a
  double press opens one tab. The launch wrapper then checks and registers in a single
  compare-and-set transaction before it execs Claude, so two tabs racing for the same
  session can't both start it.
- **The board never deletes or hides a running session's data behind its back.** There
  is no automatic clean-up of any kind; every deletion and every park comes from the
  session itself or from a button the person pressed and confirmed.
- **Park and End on a running session are requests.** The button (after a confirm that
  names the session) sets `park_requested_at` or `end_requested_at`. The session's
  monitor passes the request on, and Claude acts on it: for End it does its usual
  session-end memory save, then calls `end_session`; for Park it brings its items up to
  date, then calls `park_session`. The row shows parking or ending meanwhile. Pressing
  the button again offers **Cancel** (clears the request; if the monitor had already
  passed it on, recorded as `end_told_at` / `park_told_at`, the session also gets a
  "carry on" message) or **Force** (a second confirm naming the
  session: Force end deletes the rows without the memory save, Force park sets the
  flag). One request at a time: the other button says to cancel the first.
- **Park and End on a dead session act straight away**, after a confirm that names the
  session. Unpark is immediate. Any launch clears leftover requests, so a restored
  session is never told to end or park itself.
- **A session can vanish at any moment** (an in-session `/end`, or Force from another
  board), including while one of the board's dialogs is open. Every session action in
  the TUI goes through one guard (`session_action`): if the row has gone it says
  "session no longer exists" and repaints. The dead-session path re-checks liveness when
  the confirm is answered: if the session came back, it acts on nothing.
- **In-session `/park` and `/end`** are unchanged: the session saves what it needs, then
  acts on itself. Claude Code's own transcript is untouched either way.

## Launching

**New session** takes a working directory, an optional name, an optional ticket ref
(`#42`, `owner/repo#42`, or a Linear key such as `ABC-123`) and an opening brief. It
writes the session row, then opens a Windows Terminal tab:

```
cmd.exe /c wt.exe -w 0 new-tab --title <name> wsl.exe -d <distro> -u <user> --cd <dir> -- \
    <python> -m claude_board run <session-id>
```

`wt.exe` is a Windows execution alias that WSL can't execute directly (it resolves on
the `PATH` but does nothing), so it goes through `cmd.exe /c` as Microsoft's docs say.
cmd re-parses the line, so its metacharacters are stripped from the title and refused
in the directory. Each tab start is appended to `launch.log` next to the database,
along with any output from cmd.exe or wt.exe, so a launch that fails leaves a trace.
A brief starting with `-` gets a `Brief:` header, because claude reads a leading `-` as
an option.

A directory Claude Code doesn't trust yet shows its trust prompt in the new tab; answer
it there.

Everything else is read from the database by `claude_board run`, which then execs:

```
claude --session-id <id> | --resume <id>
       --plugin-dir <board plugin>
       --mcp-config <inline JSON: the board MCP server for this session>
       --append-system-prompt <board protocol>
       -n <name>
       [opening brief, first launch only]
```

with `BOARD_SESSION_ID`, `BOARD_DB` and `BOARD_PYTHON` in the environment. Keeping the
brief out of the `wt.exe` command line avoids its `;` command separator and Windows
quoting entirely.

`--resume` is used when a transcript for the id already exists under
`~/.claude/projects/`, otherwise `--session-id` and the brief.

## The board plugin

A static plugin directory shipped in the package, loaded per session with `--plugin-dir`:

- **Monitor** (`monitors/monitors.json`): runs `claude_board monitor` for the whole
  session. It polls the database every 2 s and prints one line per new message from the
  person. Plugin monitors deliver each printed line to Claude as a notification and
  Claude interjects when one arrives, idle or mid-task. This replaces both the
  "listener that exits to wake the session" and the `PostToolUse` hook from the design
  discussion, and it restarts with the session on resume, so no `SessionStart` hook is
  needed either.
- **Skills**: `/park` and `/end`. A plugin skill whose frontmatter sets `name` answers to
  the bare name as well as the namespaced one ("The bare `/fancy` also invokes the skill
  unless another command already uses that name", code.claude.com/docs/en/skills).
  Checked headless with `disable-model-invocation: true` as these skills use: `/park`
  resolves. `/board:park` and `/board:end` remain as fallbacks if another command takes
  the bare names.

## Data model

```
sessions  id (uuid) PK, name, ticket, brief, cwd, parked (0/1),
          created_at, launched_at,
          claude_pid, claude_start, boot_id, heartbeat_at,
          end_requested_at, park_requested_at
items     id PK, session_id FK, ref ('T3' | 'Q1' | 'A2', unique per session),
          kind (task | question | agent), title, body, status,
          created_at, updated_at
messages  id PK, session_id FK, item_ref (nullable), author (claude | person),
          body, created_at, claimed_at, delivered_at
```

Deleting a session cascades. Times are UTC ISO-8601; claim and request stamps carry
microseconds so one claim or request is never mistaken for the next. Columns added after
the first release are added on start, inside one transaction, so processes starting
together against an older database can't both add the same column.

Statuses: task `todo running blocked waiting done dropped`; question
`open answered closed`; agent `running done failed`. A person's message on an open
question marks it answered.

## MCP tools (server name `board`)

| Tool | Does |
|---|---|
| `post_item(kind, title, body, status?)` | creates T/Q/A item, returns its ref |
| `update_item(ref, status?, title?, body?, note?)` | edits; a note is appended to the item's thread |
| `get_input(ref?)` | undelivered messages from the person, or the full thread for one ref (whose undelivered messages then count as delivered) |
| `list_items(include_closed?)` | this session's items |
| `park_session()` / `end_session()` | lifecycle; `park_session` also settles a pending park request |

Delivery is claim, show, confirm. A claim is one transaction, so the monitor and
`get_input` never take the same message. The monitor confirms a message only after
printing and flushing it; if stdout has closed it releases the rest for redelivery, and
a claim abandoned by a monitor killed mid-print is retaken after 30 s. Confirm and
release touch only messages still under the caller's own claim, so a monitor whose claim
was retaken can't confirm someone else's. A thread shown by `get_input(ref)` leaves out
messages the monitor has claimed and is printing, so they aren't shown twice.

One duplicate window is left, by design: a monitor stuck in `print` for over 30 s (Claude
Code not reading its stdout) has its claim retaken by `get_input`, so when the stuck
print finally completes the message has been shown twice. The old claimer's confirm is
ignored, so the database stays right; the cost is one repeated line, in a case that
needs Claude Code itself to stall.

Writes check the session inside their transaction (`BEGIN IMMEDIATE` holds the write
lock, so the row can't vanish between check and write) and raise `SessionGone` if it
has gone. Board tools then answer "this session was force-ended on the board (or has
ended): stop using board tools", and the monitor prints the same once and exits.

`claude_board run` registers the session's pid, start time and boot id just before it
execs Claude, as a compare-and-set that fails if another live Claude holds the session.
exec keeps the pid, so the registered pid is Claude's, and a session at the trust prompt
or with a slow MCP server never looks dead. The server only writes a
heartbeat, on start and every 30 s. Tool calls run in worker threads, so the store
serialises access to its one connection with a lock.

## TUI

- **Inbox tab.** Sessions on the left (a status dot, the name, the open-question count,
  and a Cylon scanner while anything is running; parked sessions are left out). Items in the centre from
  every session, ordered: open questions, blocked or waiting tasks, running, the rest.
  Selecting a session filters; Esc clears. Detail on the right: body, thread, and an
  answer box. Ctrl+S sends (Ctrl+Enter where the terminal reports it).
- **Sessions tab.** Every session with status, name, ticket, directory, open-question
  and running counts. Restore on a dead row, parked or not (unparks only once the launch
  goes through), Restore All (dead and not parked), Park / unpark, End, New session. Park
  and End follow the lifecycle rules above.
- **Look.** Matrix green inside panels; colour and a shimmering title bar on the chrome.

## Relationship to the cache dashboard

For the prototype, `claude_dashboard.py` stays as it is: single file, stdlib only,
because the Homebrew formula installs it by copying that file. The board is a separate
project in `board/` with its own dependencies (Textual, the MCP SDK) and its own
`claude-board` command.

## Path to a single tool

The goal is one tool: the board, with the cache view as one of its tabs.

1. Package the repo as one Python project with both commands (`claude-board`, and
   `claude-dashboard` kept as an alias that opens the cache tab), installed with
   `uv tool install` instead of the Homebrew single-file copy. Point the Homebrew formula
   at the package, or retire it with a note in the README.
2. Port the cache view into a Textual tab beside Inbox and Sessions, reusing its data
   code; keep its terminal-only mode for anyone who wants it.
3. Drop the separate single-file script once the tab matches it.

## Verified Claude Code facts (v2.1.287, `claude --help` and code.claude.com docs)

| Fact | Result |
|---|---|
| `--session-id <uuid>` | exists |
| `--mcp-config <configs...>` takes files or JSON strings | exists |
| `--settings` hooks merge with user settings | true: "Hook entries merge across settings levels rather than replacing each other" (not used now) |
| `--append-system-prompt` | exists. With the default `--system-prompt-snapshot on`, a resumed session reuses the prompt recorded at first launch |
| per-session plugin | `--plugin-dir <path>`, "for this session only" |
| plugin skills are namespaced | true: `/<plugin>:<skill>` |
| plugin monitors run for the whole session and their output reaches Claude as notifications | true (docs, components page) |
| `-n, --name` | exists; sets the display name and terminal title |
| `wt.exe new-tab --title`, `-w 0` | true (Microsoft docs) |
| `wt.exe` callable directly from WSL | false: it is a 2-byte execution alias; `cmd.exe /c wt.exe` works |

## Verified end to end

A real session launched through `claude_board run` (headless, in a pty) registered its
Claude process, started the monitor, posted `T1` through the MCP server, received a
board message through the monitor while idle, acted on it, and deleted its own data
with `end_session`. A second headless run checked the registered pid directly: it was
the forked pid, `/proc/<pid>/comm` was `claude` with the `--session-id` command line,
and it was still the running process (with the board's MCP server and monitor as
children) after the trust prompt and the brief. A launch through Windows Terminal reached `claude_board run` in the
new tab (confirmed by `launch.log`).

## Open questions

1. Restore All after a reboot also catches sessions that died days ago and were never
   parked. Should Restore All only take sessions that were live in the last boot?
2. Notification size: the monitor prints answers up to 1,500 characters inline and
   points to `get_input(ref)` for longer ones. Is that the right cut-off?
3. When to take the first step of "Path to a single tool": straight after the prototype
   settles, or once the board has been in daily use for a while?
