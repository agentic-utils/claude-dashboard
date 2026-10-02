import json
import os
import time

from datetime import datetime

import pytest

from claude_wheelhouse import adopt, launch, liveness

REPO = "/home/u/my.repo"
SID = "11111111-aaaa-bbbb-cccc-000000000001"


@pytest.fixture
def proc(tmp_path):
    """A fake /proc: pid 4242 is alive, started at tick 1000."""
    d = tmp_path / "proc/4242"
    d.mkdir(parents=True)
    d.joinpath("stat").write_text("4242 (claude) S 1 " + " ".join(["0"] * 17) + " 1000")
    return tmp_path / "proc"


@pytest.fixture
def sessions(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    return d


@pytest.fixture
def projects(tmp_path):
    d = tmp_path / "projects"
    d.mkdir()
    return d


def record(**over):
    return {"type": "user", "cwd": REPO, "entrypoint": "cli", "isSidechain": False,
            "message": {"role": "user", "content": "fix the VAT rounding"}} | over


def transcript(projects, sid=SID, records=None, folder=None, age=60):
    d = projects / (folder or adopt.project_folder(REPO))
    d.mkdir(exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (records or [record()])) + "\n")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def running(sessions, sid=SID, pid=4242, start=1000):
    (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "procStart": str(start)}))


@pytest.mark.parametrize("records, title, desc", [
    ([record()], "fix the VAT rounding", "first prompt when nothing better"),
    ([record(), {"type": "ai-title", "aiTitle": "VAT rounding"}], "VAT rounding", "AI title beats the prompt"),
    ([record(), {"type": "ai-title", "aiTitle": "VAT"}, {"type": "custom-title", "customTitle": "LG fix"}],
     "LG fix", "custom title wins"),
    ([record(message={"content": "<command-name>/clear</command-name>"}), record(isMeta=True),
      record(message={"content": [{"type": "text", "text": "real   prompt\nhere"}]})],
     "real prompt here", "skips command and meta records, joins text parts"),
    ([record(message={"content": "x" * 200})], "x" * 69 + "…", "long titles are trimmed"),
])
def test_title(projects, records, title, desc):
    assert adopt.read_transcript(transcript(projects, records=records)).title == title, desc


@pytest.mark.parametrize("records, folder, cwd, desc", [
    ([record(), record(cwd="/elsewhere")], None, REPO, "the cwd matching the transcript folder, not the latest"),
    ([record(cwd="/a/b")], "-other", "/a/b", "falls back to the first cwd"),
])
def test_cwd(projects, records, folder, cwd, desc):
    assert adopt.read_transcript(transcript(projects, records=records, folder=folder)).cwd == cwd, desc


@pytest.mark.parametrize("records, desc", [
    ([record(entrypoint="sdk-cli")], "headless claude -p runs"),
    ([{"type": "summary", "summary": "x"}], "no cwd or entrypoint"),
    ([record(type="system", message=None), {"type": "last-prompt"}], "never prompted"),
    ([record(message={"content": "<command-name>/resume</command-name>"}), {"type": "last-prompt", "lastPrompt": "/resume"}],
     "only slash commands"),
])
def test_not_offered(projects, records, desc):
    assert adopt.read_transcript(transcript(projects, records=records)) is None, desc


T1, T2 = "2026-10-02T12:18:49.000Z", "2026-10-02T12:20:00.000Z"


@pytest.mark.parametrize("records, active, desc", [
    ([record(timestamp=T1), {"type": "mode"}, {"type": "permission-mode"}], T1,
     "idle records appended later don't count"),
    ([record(timestamp=T1), record(type="assistant", timestamp=T2), {"type": "system", "timestamp": "2026-10-03T00:00:00Z"}],
     T2, "the last prompt or reply, not other timestamped records"),
    ([record()], None, "no timestamps: the file's mtime"),
])
def test_last_active(projects, records, active, desc):
    p = transcript(projects, records=records)
    expected = datetime.fromisoformat(active).timestamp() if active else p.stat().st_mtime
    assert adopt.read_transcript(p).active == expected, desc


def test_reads_both_ends_of_a_big_transcript(projects, monkeypatch):
    monkeypatch.setattr(adopt, "CHUNK", 400)
    filler = [record(type="assistant", message={"content": "y" * 100}) for _ in range(30)]
    p = transcript(projects, records=[record()] + filler + [{"type": "custom-title", "customTitle": "late title"}])
    c = adopt.read_transcript(p)
    assert (c.title, c.cwd) == ("late title", REPO)


def test_candidates(store, projects, sessions, proc):
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-1")
    transcript(projects, "old", age=30 * 86400)
    transcript(projects, "newer", records=[record(timestamp=T1)], age=10)   # touched lately, active earlier
    transcript(projects, "older", records=[record(timestamp=T2)], age=100)
    transcript(projects, "open", age=5)
    transcript(projects, "headless", records=[record(entrypoint="sdk-cli")])
    (projects / adopt.project_folder(REPO) / "newer" / "subagents").mkdir(parents=True)
    (projects / adopt.project_folder(REPO) / "newer/subagents/agent-1.jsonl").write_text(json.dumps(record()))
    store.create_session(REPO, name="Rare caper", sid="older")   # tracked, its tab failed: offered again
    store.create_session(REPO, sid="open")
    store.register("open", 4242, 1000, "boot-1")                  # open in a wheelhouse tab: not offered
    running(sessions, "newer")
    found = adopt.candidates(store, projects, sessions, proc)
    assert [(c.id, c.running_pid, c.name) for c in found] == [("older", None, "Rare caper"), ("newer", 4242, "")]


@pytest.mark.parametrize("start, expected, desc", [
    (1000, {SID: 4242}, "record agrees with /proc"),
    (999, {}, "pid reused by another process"),
])
def test_running_sessions(sessions, proc, start, expected, desc):
    running(sessions, start=start)
    (sessions / "77.json").write_text(json.dumps({"pid": 77, "sessionId": "gone", "procStart": "5"}))
    (sessions / "bad.json").write_text("{not json")
    assert liveness.running_sessions(sessions, proc) == expected, desc


def candidate():
    return adopt.Candidate(SID, REPO, "LG fix", time.time(), None)


def test_adopt_refuses_a_running_session(store, sessions, proc):
    running(sessions)
    opened = []
    with pytest.raises(adopt.StillRunning, match="/exit"):
        adopt.adopt(store, candidate(), "LG", sessions, proc, open_tab=lambda s, sid: opened.append(sid))
    assert opened == [] and store.session(SID) is None


def test_adopt_registers_and_opens_the_same_session(store, sessions, proc):
    opened = []
    sid = adopt.adopt(store, candidate(), "LG", sessions, proc, open_tab=lambda s, sid: opened.append(sid))
    assert sid == SID and opened == [SID]
    s = store.session(SID)
    assert (s["cwd"], s["name"], s["adopted"]) == (REPO, "LG", 1)


@pytest.mark.parametrize("name, expected, desc", [
    ("", "Rare caper", "keeps its name"),
    ("LG", "LG", "renamed when asked"),
])
def test_adopting_a_tracked_session_reuses_its_row(store, sessions, proc, name, expected, desc):
    store.create_session(REPO, name="Rare caper", sid=SID)
    opened = []
    adopt.adopt(store, candidate(), name, sessions, proc, open_tab=lambda s, sid: opened.append(sid))
    assert opened == [SID] and len(store.sessions()) == 1, desc
    assert store.session(SID)["name"] == expected, desc


def test_a_failed_launch_leaves_no_row(store, sessions, proc):
    def boom(s, sid):
        raise RuntimeError("wt.exe missing")
    with pytest.raises(RuntimeError):
        adopt.adopt(store, candidate(), "LG", sessions, proc, open_tab=boom)
    assert store.session(SID) is None


def test_an_adopted_session_resumes_with_the_wheelhouse_flags(store, projects, sessions, proc):
    transcript(projects)
    store.create_session(REPO, name="LG", sid=SID)
    argv = launch.claude_argv(store.session(SID), python="/py",
                              resume=launch.transcript_exists(SID, projects))
    assert argv[argv.index("--resume") + 1] == SID
    assert "--plugin-dir" in argv and "--mcp-config" in argv and "--append-system-prompt" in argv
    assert argv[argv.index("--system-prompt-snapshot") + 1] == "off"


def test_run_refuses_while_the_session_runs_elsewhere(store, monkeypatch):
    store.create_session("/tmp", sid=SID)
    monkeypatch.setattr(liveness, "running_pid", lambda sid: 4242)
    monkeypatch.setattr(launch.os, "execvpe", lambda *a: pytest.fail("must not exec claude"))
    with pytest.raises(SystemExit, match="still running"):
        launch.run(SID)
