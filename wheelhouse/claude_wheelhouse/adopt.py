"""Adopt a Claude Code sessiin the wheelhouse did not launch, by handoff.

The wheelhouse lists recent transcripts from ~/.claude/projects. If the chosen session is
still running, the person types /exit in its tab first; the wheelhouse never kills it. Then
the wheelhouse registers the session under its own id and opens it in a new tab like any
restore: `claude --resume <id>` with the wheelhouse's MCP server, monitor, protocol and
/wheelhouse commands.
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import launch, liveness
from .store import Store

PROJECTS = Path.home() / ".claude/projects"
WINDOW_DAYS = 14     # transcripts touched longer ago than this aren't offered
LIMIT = 40
CHUNK = 256 * 1024   # bytes read from each end of a transcript
TITLE_WIDTH = 70


class StillRunning(RuntimeError):
    def __init__(self, pid: int):
        super().__init__(f"the session is still running (pid {pid}): type /exit in its tab first")
        self.pid = pid


@dataclass
class Candidate:
    id: str
    cwd: str
    title: str
    modified: float       # transcript mtime, epoch seconds
    running_pid: int | None


def project_folder(cwd: str) -> str:
    """How Claude Code names a project's transcript folder."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _records(data: bytes):
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:   # a line cut at a chunk boundary
            continue
        if isinstance(rec, dict):
            yield rec


def _prompt_text(rec) -> str:
    """The person's typed prompt, or '' for tool results, meta and command records."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return ""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    text = (content or "").strip() if isinstance(content, str) else ""
    return "" if text.startswith("<") else text


def _one_line(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TITLE_WIDTH else text[:TITLE_WIDTH - 1] + "…"


def read_transcript(path: Path) -> Candidate | None:
    """None for transcripts not worth offering: headless runs (claude -p) and empty ones."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(CHUNK)
        if size > 2 * CHUNK:
            f.seek(size - CHUNK)
        tail = f.read() if size > CHUNK else b""
    head_recs, tail_recs = list(_records(head)), list(_records(tail))
    entry = next((r["entrypoint"] for r in head_recs + tail_recs if r.get("entrypoint")), None)
    if entry != "cli":
        return None
    cwds = [r["cwd"] for r in head_recs + tail_recs if r.get("cwd")]
    if not cwds:
        return None
    # resume must run where the transcript lives; the session may have cd'd elsewhere since
    cwd = next((c for c in cwds if project_folder(c) == path.parent.name), cwds[0])
    found = {}
    for rec in head_recs + tail_recs:   # later records win
        for key in ("customTitle", "aiTitle", "lastPrompt"):
            if rec.get(key):
                found[key] = rec[key]
    first = next((t for t in map(_prompt_text, head_recs) if t), "")
    title = found.get("customTitle") or found.get("aiTitle") or first or found.get("lastPrompt") or ""
    return Candidate(path.stem, cwd, _one_line(title), path.stat().st_mtime, None)


def candidates(store: Store, projects: Path = PROJECTS, sessions: Path = liveness.SESSIONS,
               proc: Path = liveness.PROC, now: float | None = None) -> list[Candidate]:
    """Recent interactive sessions the wheelhouse doesn't track yet, newest first."""
    tracked = {s["id"] for s in store.sessions()}
    cutoff = (now or time.time()) - WINDOW_DAYS * 86400
    paths = []
    for p in projects.glob("*/*.jsonl"):   # subagent transcripts sit a level deeper
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if p.stem not in tracked and mtime >= cutoff:
            paths.append((mtime, p))
    alive = liveness.running_sessions(sessions, proc)
    found = []
    for _, p in sorted(paths, reverse=True):
        try:
            c = read_transcript(p)
        except OSError:
            continue
        if c:
            c.running_pid = alive.get(c.id)
            found.append(c)
            if len(found) == LIMIT:
                break
    return found


def adopt(store: Store, c: Candidate, name: str, sessions: Path = liveness.SESSIONS,
          proc: Path = liveness.PROC, open_tab=launch.open_tab) -> str:
    """Register the session and open it in a new tab. Refuses while it is still running."""
    pid = liveness.running_pid(c.id, sessions, proc)
    if pid:
        raise StillRunning(pid)
    sid = store.create_session(c.cwd, name=name, sid=c.id)
    try:
        open_tab(store, sid)
    except BaseException:
        store.end(sid)   # nothing was launched: don't leave a half-adopted row behind
        raise
    return sid
