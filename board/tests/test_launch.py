import json

import pytest

from claude_board import launch


def row(**over):
    return {"id": "abc-123", "name": "demo", "ticket": "#7", "brief": "fix it", "cwd": "/home/u/repo"} | over


@pytest.mark.parametrize("over, title, cwd, desc", [
    ({}, "demo", "/home/u/repo", "named session"),
    ({"name": ""}, "repo", "/home/u/repo", "falls back to the directory name"),
    ({"name": "a;b", "cwd": "/x;y"}, "a,b", r"/x\;y", "semicolons can't split the wt command"),
    ({"name": "fish & chips"}, "fish  chips", "/home/u/repo", "cmd metacharacters dropped from the title"),
])
def test_wt_argv(over, title, cwd, desc):
    argv = launch.wt_argv(row(**over), python="/py", distro="Ubuntu", user="u")
    assert argv[:8] == ["cmd.exe", "/c", "wt.exe", "-w", "0", "new-tab", "--title", title], desc
    assert argv[argv.index("--cd") + 1] == cwd, desc
    assert argv[-5:] == ["/py", "-m", "claude_board", "run", "abc-123"], desc


@pytest.mark.parametrize("over, resume, has, lacks, last, desc", [
    ({}, False, ["--session-id", "-n"], ["--resume"], "Ticket: #7\n\nfix it", "first launch takes the brief"),
    ({}, True, ["--resume", "-n"], ["--session-id"], None, "restore resumes without the brief"),
    ({"name": "", "ticket": "", "brief": ""}, False, ["--session-id"], ["-n"],
     "Session started from the board. Wait for instructions.", "bare launch"),
])
def test_claude_argv(over, resume, has, lacks, last, desc):
    argv = launch.claude_argv(row(**over), python="/py", resume=resume)
    for flag in has:
        assert flag in argv, desc
    for flag in lacks:
        assert flag not in argv, desc
    if last:
        assert argv[-1] == last, desc
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["board"]
    assert mcp["env"]["BOARD_SESSION_ID"] == "abc-123"
    assert argv[argv.index("--plugin-dir") + 1] == str(launch.PLUGIN_DIR)


def test_wt_argv_refuses_cmd_metacharacters_in_the_directory():
    with pytest.raises(ValueError, match="can't launch"):
        launch.wt_argv(row(cwd="/home/u/a&b"), python="/py", distro="Ubuntu", user="u")


def test_transcript_exists(tmp_path):
    (tmp_path / "-home-u-repo").mkdir()
    (tmp_path / "-home-u-repo/abc-123.jsonl").write_text("{}")
    assert launch.transcript_exists("abc-123", tmp_path)
    assert not launch.transcript_exists("other", tmp_path)


def test_open_tab_refuses_a_running_session(store, sid, monkeypatch):
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: True)
    with pytest.raises(RuntimeError, match="already running"):
        launch.open_tab(store, sid)


def test_a_brief_starting_with_a_dash_is_not_read_as_an_option():
    """`claude "-x"` fails with "unknown option" (review #3)."""
    argv = launch.claude_argv(row(ticket="", brief="- first\n- second"), python="/py", resume=False)
    assert not argv[-1].startswith("-") and argv[-1].endswith("- first\n- second")


def test_run_registers_its_own_pid_before_exec(store, sid, tmp_path, monkeypatch):
    """The pid survives exec, so it is Claude's: no gap before the MCP server starts (review #1)."""
    class Exec(Exception):
        pass

    def fake_exec(*a):
        raise Exec
    monkeypatch.setattr(launch.os, "execvpe", fake_exec)
    monkeypatch.setattr(launch, "transcript_exists", lambda sid: False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exec):
        launch.run(sid)
    row_ = store.session(sid)
    assert row_["claude_pid"] == launch.os.getpid()
    assert launch.liveness.is_alive(row_["claude_pid"], row_["claude_start"], row_["boot_id"])


def test_open_tab_logs_the_launcher_output(store, sid, monkeypatch):
    """A failing cmd.exe or wt.exe must leave a trace (review #11)."""
    calls = []
    monkeypatch.setattr(launch.liveness, "is_alive", lambda *a: False)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    launch.open_tab(store, sid)
    (argv, kw), = calls
    assert kw["stdout"].name == str(store.path.parent / "launch.log")
    assert kw["stderr"] == launch.subprocess.STDOUT
    assert "wt.exe" in (store.path.parent / "launch.log").read_text()
