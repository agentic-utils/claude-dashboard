"""Open sessions in Windows Terminal tabs, and build the Claude command inside them.

The wt.exe command line carries only the session id: `claude_board run <id>` reads
everything else from the database. That keeps the brief away from wt's `;`
command separator and from Windows argument quoting.

wt.exe is a Windows execution alias, which WSL can't run directly, so it goes
through `cmd.exe /c` as Microsoft's docs prescribe. cmd re-parses the line, so
its metacharacters are kept out of the title and refused in the directory.
"""

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from . import liveness
from .store import Store, db_path, now

PLUGIN_DIR = Path(__file__).parent / "plugin"
PROTOCOL = (Path(__file__).parent / "protocol.md").read_text()


CMD_META = set('&|<>^%"')


def wt_argv(session, *, python: str, distro: str, user: str) -> list[str]:
    if CMD_META & set(session["cwd"]):
        raise ValueError(f"can't launch in {session['cwd']}: it contains one of {''.join(sorted(CMD_META))}")
    title = "".join(c for c in session["name"] or Path(session["cwd"]).name if c not in CMD_META)
    return [
        "cmd.exe", "/c", "wt.exe", "-w", "0", "new-tab", "--title", title.replace(";", ","),
        "wsl.exe", "-d", distro, "-u", user, "--cd", session["cwd"].replace(";", r"\;"),
        "--", python, "-m", "claude_board", "run", session["id"],
    ]


def transcript_exists(sid: str, projects: Path = Path.home() / ".claude/projects") -> bool:
    return any(projects.glob(f"*/{sid}.jsonl"))


def claude_argv(session, *, python: str, resume: bool) -> list[str]:
    mcp = {"mcpServers": {"board": {
        "command": python, "args": ["-m", "claude_board", "mcp"],
        "env": {"BOARD_SESSION_ID": session["id"], "BOARD_DB": str(db_path())},
    }}}
    argv = ["claude", "--resume" if resume else "--session-id", session["id"],
            "--plugin-dir", str(PLUGIN_DIR),
            "--mcp-config", json.dumps(mcp),
            "--append-system-prompt", PROTOCOL]
    if session["adopted"]:
        # Claude records the system prompt at a conversation's first request and replays it
        # on every resume, so an adopted session would never see the protocol. "off" renders
        # it fresh each request; it has to stay off, since the old record outlives one launch.
        argv += ["--system-prompt-snapshot", "off"]
    if session["name"]:
        argv += ["-n", session["name"]]
    if not resume:
        argv.append(opening_prompt(session))
    return argv


def opening_prompt(session) -> str:
    lines = []
    if session["ticket"]:
        lines.append(f"Ticket: {session['ticket']}")
    lines.append(session["brief"] or "Session started from the board. Wait for instructions.")
    prompt = "\n\n".join(lines)
    # claude reads a leading "-" as an option ("unknown option"), e.g. a pasted bullet list
    return f"Brief:\n{prompt}" if prompt.startswith("-") else prompt


def open_tab(store: Store, sid: str) -> None:
    """Launch (or restore) a session in a new tab. Refuses if it is already running."""
    session = store.session(sid)
    if session is None:
        raise KeyError(sid)
    if liveness.is_alive(session["claude_pid"], session["claude_start"], session["boot_id"]):
        raise RuntimeError(f"session {session['name'] or sid} is already running")
    if liveness.status(session) == "starting":   # a tab is opening but hasn't registered yet
        raise RuntimeError(f"session {session['name'] or sid} is still starting")
    argv = wt_argv(session, python=sys.executable,
                   distro=os.environ.get("WSL_DISTRO_NAME", "Ubuntu"), user=getpass.getuser())
    store.mark_launched(sid)
    # cmd.exe or wt.exe failing would otherwise be invisible: keep their output
    with open(store.path.parent / "launch.log", "a") as log:
        print(now(), sid, "launching", " ".join(argv), file=log, flush=True)
        # cmd.exe warns about (and ignores) a \\wsl$ working directory, so start it from C:
        subprocess.Popen(argv, cwd="/mnt/c", stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)


def run(sid: str) -> None:
    """Runs inside the new tab: final liveness check, then exec Claude."""
    store = Store()
    session = store.session(sid)
    if session is None:
        sys.exit(f"claude-board: no session {sid} (ended?)")
    env = dict(os.environ, BOARD_SESSION_ID=sid, BOARD_DB=str(store.path), BOARD_PYTHON=sys.executable)
    os.chdir(session["cwd"])
    resume = transcript_exists(sid)
    argv = claude_argv(session, python=sys.executable, resume=resume)
    with open(store.path.parent / "launch.log", "a") as log:   # a tab that dies on start leaves this behind
        print(now(), sid, "resume" if resume else "new", session["cwd"], file=log)
    # exec keeps this pid, so it is Claude's: registering now leaves no window (trust prompt,
    # slow MCP start) in which a live session looks dead and could be restored twice.
    # Check-and-register is one transaction, so two tabs racing can't both get here.
    other = liveness.running_pid(sid)   # e.g. adopted, but its old tab never ran /exit
    if other:
        sys.exit(f"claude-board: session {session['name'] or sid} is still running (pid {other}): "
                 "type /exit in its tab, then restore it from the board")
    pid = os.getpid()
    if not store.register_if_free(sid, pid, liveness.start_time(pid), liveness.boot_id(), liveness.is_alive):
        sys.exit(f"claude-board: session {session['name'] or sid} is already running in another tab")
    os.execvpe("claude", argv, env)
