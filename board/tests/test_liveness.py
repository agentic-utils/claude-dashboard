from datetime import datetime, timedelta, timezone

import pytest

from claude_board import liveness

NOW = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
BOOT = "boot-a"


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat(timespec="seconds")


@pytest.fixture
def proc(tmp_path):
    """A fake /proc: pid 4242 is a 'claude' started at tick 1000, its child 4243 is python."""
    (tmp_path / "sys/kernel/random").mkdir(parents=True)
    (tmp_path / "sys/kernel/random/boot_id").write_text(BOOT + "\n")
    for pid, ppid, name in ((4242, 1, "claude"), (4243, 4242, "python3")):
        d = tmp_path / str(pid)
        d.mkdir()
        fields = ["S", str(ppid)] + ["0"] * 17 + ["1000"]
        d.joinpath("stat").write_text(f"{pid} ({name}) " + " ".join(fields))
        d.joinpath("comm").write_text(name + "\n")
    return tmp_path


def session(**over):
    base = dict(parked=0, claude_pid=4242, claude_start=1000, boot_id=BOOT,
                heartbeat_at=ago(10), launched_at=ago(600))
    return base | over


@pytest.mark.parametrize("over, waking, expected, desc", [
    ({}, False, "live", "process running, fresh heartbeat"),
    ({"heartbeat_at": ago(600)}, False, "stalled", "running but quiet"),
    ({"heartbeat_at": ago(600)}, True, "live", "quiet just after waking from sleep"),
    ({"parked": 1}, False, "parked", "parked wins"),
    ({"claude_start": 999}, False, "dead", "pid reused by another process"),
    ({"boot_id": "boot-old"}, False, "dead", "machine rebooted"),
    ({"claude_pid": 9999}, False, "dead", "process gone"),
    ({"claude_pid": None, "heartbeat_at": None, "launched_at": ago(10)}, False, "starting", "just launched"),
    ({"claude_pid": None, "heartbeat_at": None, "launched_at": ago(300)}, False, "dead", "launch never registered"),
    ({"claude_pid": 9999, "heartbeat_at": ago(900), "launched_at": ago(5)}, False, "starting", "restore in progress"),
    ({"claude_pid": None, "heartbeat_at": None, "launched_at": None}, False, "dead", "never launched"),
])
def test_status(proc, over, waking, expected, desc):
    assert liveness.status(session(**over), now=NOW, waking=waking, proc=proc) == expected, desc


def test_find_claude_walks_up_to_the_claude_process(proc):
    assert liveness.find_claude(4243, proc) == 4242


@pytest.mark.parametrize("steps, expected, desc", [
    ([(1, 1)], False, "clocks agree"),
    ([(3600, 1)], True, "wall clock jumped an hour: slept"),
    ([(3600, 1), (1, 61)], False, "grace over after a minute"),
])
def test_wake_detector(steps, expected, desc):
    wall, mono = [1000.0], [50.0]
    w = liveness.WakeDetector(clock=lambda: wall[0], mono=lambda: mono[0])
    for dw, dm in steps:
        wall[0] += dw
        mono[0] += dm
        result = w.tick()
    assert result is expected, desc
