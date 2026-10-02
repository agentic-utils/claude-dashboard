# claude-wheelhouse: suite architecture

Issue: #57. Status: design, for review before any code moves.

claude-wheelhouse is a small suite of terminal tools for running many Claude
Code sessions at once. The wheelhouse itself launches and tracks sessions and
gathers their tasks and questions into one inbox. Optional modules plug into
it as panes, so one window shows everything that needs attention.

## Goals

1. **One hub, optional modules.** `claude-wheelhouse` is the one package
   everyone installs: sessions, inbox, store, the wheelhouse app. Review and
   dashboard are separate packages that plug into it; install only the ones you
   want. Each module runs inside the wheelhouse and depends on it.
2. **A thin host.** The wheelhouse app hosts panes and owns no module data.
   The hub's own data is sessions and their threads; everything a module
   knows stays in that module.
3. **Surface-agnostic content.** Every package serves its content through a
   service layer, so a browser surface can be added later without touching
   module logic. The browser surface itself is not in scope.
4. **Wheelhouse reports, never directs.** Everything wheelhouse injects into a
   session (`protocol.md`, the MCP tool descriptions, the `/wheelhouse` skill)
   tells the model where and how to report, and nothing about how to work.
   The user's own instructions (CLAUDE.md and the like) stay the only source
   of working practice; wheelhouse never overrides them or adds to them. In
   Doug's words: "Wheelhouse must NEVER override a user's instructions in
   CLAUDE.md. It does not dictate that certain practices will be followed; it
   ONLY indicates how to report practices of a certain type, should the
   user's CLAUDE.md instruct it to behave in that way."
   - Where wheelhouse defines a kind of report, such as autonomous decisions
     (claude-wheelhouse issue #61), the injected text says explicitly that it
     applies only when the user's instructions already have the model work
     that way, and otherwise there is nothing to report.
   - Naming a way of working in context nudges the model towards it, so any
     such section is checked for that before it ships: A/B headless runs of
     the same tasks with and without the section, comparing how the model
     behaves, not just what it reports.

Non-goals: a generic, schema-driven UI that renders on both surfaces; remote
access; anything that spans Linux users (one instance per user, as today).

## Packages

| Package | Role | Today |
|---|---|---|
| `claude-wheelhouse` | The hub: sessions (launch, adopt, liveness, park, end), inbox of tasks and questions, store, MCP server, monitor, the `claude-wheelhouse` CLI, the wheelhouse app, theme (colour, shimmer, Cylon, Matrix cursor), the Pane and Service protocols, shared session views | `board/` (PR #56) plus theme pieces of `claude_dashboard.py` |
| `claude-wheelhouse-review` | Open PRs and unopened branches, with merge/draft/close actions | The PR tab inside `claude_dashboard.py` |
| `claude-wheelhouse-dashboard` | Cache-token dashboard | `claude_dashboard.py` live/history views |

PyPI names checked on 2026-10-02: `claude-wheelhouse` is free.
`claude-board`, `claude-review` and `claude-dashboard` are taken, which is why
each module package carries the full prefix.

### Workspace layout

One repo (`agentic-utils/claude-wheelhouse`, renamed from `claude-dashboard`),
one uv workspace:

```
claude-wheelhouse/
  pyproject.toml            [tool.uv.workspace] members = ["packages/*"]
  LICENSE                   MIT
  packages/
    wheelhouse/             claude-wheelhouse
      src/claude_wheelhouse/
    review/                 claude-wheelhouse-review
      src/claude_wheelhouse_review/
    dashboard/              claude-wheelhouse-dashboard
      src/claude_wheelhouse_dashboard/
```

Each package has its own `pyproject.toml`, tests and version. Module packages
depend on `claude-wheelhouse` and nothing else in the workspace. The hub
finds modules at runtime (below), so installing it does not drag every module
in.

Install with `uv tool install` (Python 3.12+), from git until we publish to
PyPI:

```
uv tool install "claude-wheelhouse @ git+https://github.com/agentic-utils/claude-wheelhouse#subdirectory=packages/wheelhouse" \
    --with "claude-wheelhouse-review @ git+...#subdirectory=packages/review" \
    --with "claude-wheelhouse-dashboard @ git+...#subdirectory=packages/dashboard"
```

That is long. The README gets a copy-paste block, and a `claude-wheelhouse
install <module>` helper can come later if it earns its keep.

## The two layers in every package

```
+---------------------------------------------+
| surfaces                                    |
|   tui/  Textual widgets (now)               |
|   web/  HTTP + websocket (later, not built) |
+----------------------+----------------------+
                       | calls
+----------------------v----------------------+
| service                                     |
|   queries   -> JSON-able dataclasses        |
|   commands  -> typed errors                 |
|   changes() -> "something moved, redraw"    |
+----------------------+----------------------+
                       |
+----------------------v----------------------+
| own SQLite database per package             |
|   in ~/.local/state/claude-wheelhouse/      |
+---------------------------------------------+
```

The rule that makes this work: **no logic in widgets.** A widget renders what
a query returned and turns a click into a command call. Everything that
decides anything lives in the service.

### Service

- **Queries** return frozen dataclasses of plain types (str, int, bool,
  ISO-8601 UTC strings, lists of the same). `dataclasses.asdict()` gives JSON,
  so a web adapter returns them as they are. Examples: `inbox() ->
  list[ThreadView]`, `sessions() -> list[SessionView]`, `prs() ->
  list[PrRow]`.
- **Commands** are named methods that validate their own inputs and raise
  typed errors from the hub's small hierarchy (`WheelhouseError`, with
  subclasses such as `SessionGone`, `StillStarting`, `NotAllowed`). The TUI
  turns them into toasts; a web adapter turns them into 404/409/403. Examples:
  `park(session_id)`, `send(thread_id, text)`, `restore(session_id)`,
  `merge(repo, number)`.
- **Change feed.** `changes()` yields when the package's data may have moved.
  For SQLite packages it polls `PRAGMA data_version` on the service's own
  read connection (this counter moves only for commits made by *other*
  connections, which is exactly the MCP servers and monitors writing behind
  the TUI's back; the service bumps the feed itself after its own commands).
  Surfaces redraw on a tick; they never diff data themselves.

### Surfaces

A pane declares its surfaces in a dict, keyed by surface name (see
"Module protocol"):

```python
surfaces = {"tui": make_review_widget}     # later: "web": make_review_router
```

A surface picks the key it knows. Each surface hand-writes its own views
against the same service. The duplication is real but cheap and readable,
which a generic renderer would not be.

**Free browser stopgap.** `textual-serve` runs the whole wheelhouse app in a
browser tab with no extra code. It is not a web surface (it streams the
terminal UI), but it answers "I want this in a browser tab" until there is
demand for a real one.

## Module protocol

Anyone can add a module, the review and dashboard modules included, by
writing a package against one public protocol. The hub has no private hooks
for its own modules: if review needs something, it goes into the protocol.

### Discovery

A module is a Python package that declares one entry point in the group
`claude_wheelhouse.modules`, pointing at its module object:

```toml
[project.entry-points."claude_wheelhouse.modules"]
review = "claude_wheelhouse_review:module"
```

Installing the package (for example with `uv tool install ... --with`) is
all it takes. The hub reads the entry points at startup; there is no config
file to edit and nothing to register by hand. `claude-wheelhouse modules`
lists what it found: id, package version, API version and status (loaded,
incompatible, failed, with the reason).

### The module object

Only the metadata is required. Every other slot is optional, so a module can
be a single pane, a single CLI subcommand, or both.

```python
class Module(Protocol):
    id: str                 # "review"; also its tab, CLI word and DB name
    title: str              # "Review"
    version: str            # the package version
    api: int                # the WHEELHOUSE_API it was written against

    # optional slots
    panes: list[Pane]                      # tabs in the wheelhouse app
    cli: Callable[[Context, list[str]], int] | None   # claude-wheelhouse <id> ...
    service: Service | None                # queries, commands, changes()
    def start(self, ctx: Context) -> None: ...        # open DB, start refreshers
    def stop(self) -> None: ...                       # flush and release

class Pane(Protocol):
    title: str
    surfaces: dict[str, Callable[[Context], object]]  # {"tui": make_widget}
    bindings: list[Binding]
    def badge(self) -> Badge | None: ...              # count + severity, or None
```

Rules a module follows:

- **Service first.** Queries return JSON-able dataclasses; commands raise
  errors from the hub's hierarchy (`WheelhouseError` and subclasses); the
  change feed says when to redraw (see "Service" above). Widgets hold no
  logic.
- **Its own data.** A module keeps its database at
  `<state dir>/<id>.db` and never writes anyone else's. What it wants to
  share, it publishes as read-only SQLite views named `wh_<id>_*` (see
  "Data ownership").
- **Badges are cheap.** `badge()` runs on the change-feed tick and must not
  touch the network.
- **No cross-imports.** A module imports `claude_wheelhouse` and nothing from
  another module. Modules meet only through published views.

The hub's own inbox and sessions views are panes built the same way, so they
get the same isolation.

**Badges are the point of the app.** The tab bar reads
`Inbox 3 . Review 1 . Dashboard !` so the user sees where attention is
needed without visiting each tab.

### What the hub gives a module

One `Context` object, passed to `start`, `cli` and every surface factory:

- `state_dir`: where the module keeps its database and cache.
- `sessions()`: read-only access to the hub's `wh_sessions` view.
- `views(module_id)`: read-only access to another module's published views,
  treating a missing database or view as "nothing known".
- `changes()`: the hub's change feed, to redraw when sessions move.
- `notify(text, severity)`: a toast in whichever surface is showing.
- `config`: the module's own section of the user's config.

Nothing else. If a module needs more, the protocol grows (with an API bump if
it breaks anyone).

The hub alone decides what goes into the sessions it launches: the MCP
server, the monitor, the protocol text and the `/wheelhouse` skill. In v1 a
module does not add MCP tools, skills or protocol text to a session. This
slot opens when a real module needs it.

### Compatibility and isolation

The hub declares `WHEELHOUSE_API`, a single integer, bumped on any breaking
change and matched exactly (no version ranges). A module whose `api`
differs is not started: its tab shows an error card saying which version it
needs and which the hub has. A module that fails to import, raises in
`start`, or raises inside its widget subtree gets the same treatment: an error
card with the reason, a summary of the traceback and a retry key. The other
modules and the app keep running. Textual's default of tearing down the whole
app on an uncaught exception is overridden by a container around each pane.

### Contributor kit

- **An example module** in the repo (`examples/hello/`): one pane with a
  badge, one CLI subcommand, a tiny service and its own database. It is the
  template a contributor copies.
- **Conformance tests** in `claude_wheelhouse.testing`, which a module runs
  in its own suite: entry point resolves, metadata present, `api` matches,
  `start`/`stop` with a temporary state dir, every surface factory builds,
  `badge()` returns quickly without network, published views match `wh_<id>_*`.
- Review and dashboard are written against the same protocol and pass the
  same tests, which keeps the protocol honest.
- A contributor starts by copying the example module. A scaffold command
  (`claude-wheelhouse new-module`) waits until copying stops being enough.

## Data ownership

Each package owns its own SQLite database under
`~/.local/state/claude-wheelhouse/`: the hub's `wheelhouse.db`, and
`<id>.db` for each module, with the durability rules already proven in the
prototype (WAL, `synchronous=FULL`, one transaction per write). No package
writes another package's database.

Packages do share one concept: **the Claude Code session**. Sessions open PRs,
the hub tracks sessions, the cache view is per session. The hub defines the
shared key (`session_id`, the Claude Code session UUID) and a tiny read-only
publishing convention:

- The hub exposes what it knows about sessions (name, ticket, directory,
  state) as a SQLite view `wh_sessions` with `session_id` as its first column.
- A module that knows something about sessions exposes it the same way, as
  a view named `wh_<id>_sessions`, so the hub's sessions view can show it.
  Any other view a module publishes is also named `wh_<id>_*`.
- A reader opens the other database read-only (`file:...?mode=ro` URI)
  through a hub helper and treats a missing file or view as "not installed
  yet" or "nothing known".

So the review pane can show "opened by session *Rare caper*" by reading the
hub's `wh_sessions` view, and the sessions view can show a session's open PRs
from `wh_review_sessions`.

## The `claude-wheelhouse` CLI and the slash commands

There are two entry points, and they live in different places:

- **The `claude-wheelhouse` CLI** runs from a plain terminal. It is how a
  user opens the wheelhouse, and how they restart it if it fails.
- **The `/wheelhouse` slash commands** exist only inside Claude Code sessions
  that the wheelhouse launched. They are how a session talks to the
  wheelhouse about its own lifecycle.

Sessions start only from the sessions view's New session button (or an Adopt,
below), because launching is what injects the slash commands and the session
protocol. So the CLI starts the app, the app starts sessions, and sessions
carry the slash commands.

### The CLI

One CLI, owned by the hub:

```
claude-wheelhouse              the wheelhouse app
claude-wheelhouse review       the review pane on its own
claude-wheelhouse dashboard    the cache dashboard on its own
claude-wheelhouse modules      list installed modules and their status
claude-wheelhouse review ...   module subcommands pass through
```

Modules add subcommands through the `cli` slot of the module protocol
(below). There are no other alias scripts.

### Slash commands in sessions

The wheelhouse injects its lifecycle commands into the sessions it launches
(via `--plugin-dir`, as now) as **one skill** whose frontmatter `name:` is
`wheelhouse`, reading `$ARGUMENTS`:

```
/wheelhouse park     I might resume this next week
/wheelhouse end      truly finished, dispose of all data
```

Inside a session the short name reads naturally: you are in Claude, calling
Claude's wheelhouse. One skill leaves room for `/wheelhouse status` and
friends without another plugin entry each time. The name is fixed; there is
no clash detection or renaming. The plugin is namespaced as `wheelhouse`, so
`/wheelhouse:wheelhouse` still reaches it if something else ever claims the
bare name.

## Adopting a running session

A session started outside the wheelhouse has no injected commands or
protocol. The wheelhouse adopts it by handoff:

1. The wheelhouse finds the session's id from its transcript under
   `~/.claude/projects/` and lists it with an **Adopt** button.
2. Adopt asks the user to type `/exit` in that session.
3. Once the session has exited, the wheelhouse opens a new terminal tab
   running `claude --resume <id>` with the usual injected flags, and tracks
   it from then on.

The conversation carries over intact; only the process is replaced.

**Deferred: hot adoption.** Attaching to a session without restarting it
(through a CLI the session calls, plus the Monitor tool) would avoid the
`/exit`, but is not designed or spiked yet.

## What moves where

### Hub (`board/` to `packages/wheelhouse`)

The prototype's `board/claude_board/` becomes `claude_wheelhouse`, and the
"board" name is dropped everywhere: the database moves to
`~/.local/state/claude-wheelhouse/wheelhouse.db`, environment variables
become `WHEELHOUSE_*`, and the MCP server and injected plugin are both named
`wheelhouse`.

`store.py` is already most of a service. Out of `tui.py` and into the service:

- `session_action`, the shared "does this session still exist" guard, becomes
  the service's own precondition on every session command (raises
  `SessionGone`).
- the liveness re-check in `act_on_dead` becomes part of the `restore` and
  dead-session `park`/`end` commands.
- the Park and End decision table (running: set a request; dead: act now;
  cancel and force) moves into commands, so the rule that the wheelhouse
  never deletes or hides a running session's data behind its back is enforced
  in one place any surface must go through.

The TUI keeps dialogs, confirm wording and layout.

### Review (PR tab of `claude_dashboard.py` to `packages/review`)

The tab is proven useful and slow to refresh. Why, from the code at `7d219e4`:

- **One `gh` subprocess per PR.** `collect_prs` (`claude_dashboard.py:2417`)
  searches for open PRs (up to `PR_SEARCH_LIMIT = 200`, line 2115), then runs
  `gh pr view` once per PR in `_pr_row` (line 2166), eight at a time
  (`PR_WORKERS = 8`, line 2114). Each call is a fresh `gh` process: Go
  startup, auth lookup, one network round trip.
- **Cached PRs are fetched twice per scan.** With a cache, every cached PR is
  re-checked first (line 2462) and then fetched again when the search returns
  it (line 2479).
- **The branch scan multiplies.** For each of up to `BRANCH_REPO_LIMIT = 10`
  repos, `_branch_rows` (line 2213) calls `repos/{repo}`, lists branches, then
  makes one `compare` call per branch, up to `BRANCH_LIMIT_PER_REPO = 50`. That
  is up to about 520 calls on top of the PR detail calls.

Inference, not measured: with a hundred open PRs a full scan is a few hundred
process spawns and round trips, and call count, not GitHub's response time,
dominates. The first step of the rewrite is to time one scan with call counts
logged, to confirm before redesigning around it.

The rewrite:

- **One GraphQL query for all PR detail.** A `search(type: ISSUE)` query with
  the row's fields inline (`reviewDecision`, `statusCheckRollup`,
  `commits(last: 1)`, `comments(last: 1)`, `mergeable`, `mergeStateStatus`,
  `isDraft`, `headRefName`, and `repository { viewerPermission }` for the push
  check), paged 100 at a time. That replaces 1 + N calls with 1 or 2. The
  dashboard's cleanup command already uses this search shape
  (`claude_dashboard.py:2324`).
- **Branches via GraphQL too.** `Ref.compare` gives `aheadBy` and the tip
  commit's author per branch inside one query per repo (to verify against the
  live schema before building on it).
- **A background refresher writing to the module's cache DB.** The pane only
  ever reads the cache, so it opens instantly with the last known state and
  the change feed repaints rows as the refresher lands them. Mutating actions
  (merge, draft toggle, close, delete branch) become service commands that
  update the cache optimistically, as `apply_pr_action_locally` does today.
- Rate-limit headroom and offline handling carry over as they are.

### Dashboard (rest of `claude_dashboard.py` to `packages/dashboard`)

The dashboard is a 5,200-line, stdlib-only, raw-terminal (termios) program.
It is not a Textual app, so it cannot simply be hosted as a pane. The order:

1. Move the file into `packages/dashboard` unchanged, runnable as
   `claude-wheelhouse dashboard`. In the wheelhouse app it appears as a card that
   opens it in its own terminal tab, rather than an embedded pane.
2. Split its scan and usage logic into a service (it is already largely
   separate from rendering).
3. Port the views to Textual so the cache view embeds as a real pane. This is
   a near-term follow-up, to revisit soon after the app is in daily use.

The suite targets Python 3.12+ with Textual, so the dashboard's stdlib-only
and Python 3.9 constraints go.

## Packaging and licence

- **Licence: MIT.** A `LICENSE` file at the repo root (copyright 2026 Doug
  Lindsay), declared per PEP 639 in every package:
  `license = "MIT"` and `license-files = ["LICENSE"]` (each package points at
  the root file or carries a copy, whichever the build backend supports
  cleanly). The repo is public with no licence today, which legally means
  nobody may reuse it; this fixes that.
- **README disclaimer**, near the top: claude-wheelhouse is an independent
  community tool for people who use Claude Code. It is not affiliated with,
  endorsed by or supported by Anthropic. "Claude" is a trademark of
  Anthropic, PBC.
- **Distribution:** `uv tool install` from git now, Python 3.12+. Publish to
  PyPI once the package boundaries have settled; no name reservation in the
  meantime.
- **Homebrew tap retired.** The `agentic-utils/tap` formula and the
  dashboard's self-updater are replaced by `uv tool install` (and
  `uv tool upgrade`). The last tap release points existing users at the new
  install command.

## Migration order

Each step is its own issue and PR, so every review covers one kind of change.

1. **Finish PR #56 (the prototype).** Switch `/park` and `/end` to the
   single `/wheelhouse park|end` skill with the fixed name, then the
   round-5 review, then find the flaky test. Adoption by handoff and the
   full rename (the `claude-wheelhouse` package and command, the
   `claude_wheelhouse` module, the database under
   `~/.local/state/claude-wheelhouse/`, `WHEELHOUSE_*` environment
   variables, and the MCP server and plugin named `wheelhouse`) land early
   on PRs stacked on #56, so the prototype can be used day to day.
2. **Restructure into the workspace.** Move the prototype into
   `packages/wheelhouse`. Add the theme, the module protocol, `Context`,
   `claude_wheelhouse.testing`, the example module and the session views to
   the hub, move the dashboard file in
   unchanged, add LICENSE and the README disclaimer. Move the session logic
   out of `tui.py` into its service. Retire the Homebrew tap.
3. **Review module.** Lift the PR tab out of the dashboard into
   `packages/review` as a Textual module, with the GraphQL refresher.
4. **The wheelhouse app.** Module discovery, `claude-wheelhouse modules`,
   tab bar with badges, pane isolation, the dashboard card.
5. **Dashboard Textual port** (near-term follow-up).

Later: hot adoption, a web surface, PyPI.
