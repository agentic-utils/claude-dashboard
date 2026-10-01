"""Is a session alive? Decided by its Claude process, never by the heartbeat.

Sleep and hibernate keep the process, so the session stays live. A reboot or a
WSL shutdown changes the boot id, so it is dead. The process start time guards
against pid reuse. The heartbeat only feeds the 'stalled' hint.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

PROC = Path("/proc")
STARTING_GRACE = 90      # seconds a launched session may take to register
STALLED_AFTER = 120      # heartbeat age that suggests a stalled session
CLOCK_JUMP = 30          # wall clock ahead of monotonic by this much means we slept
WAKE_GRACE = 60          # hold off the stalled hint this long after waking


def boot_id(proc: Path = PROC) -> str:
    return (proc / "sys/kernel/random/boot_id").read_text().strip()


def start_time(pid: int, proc: Path = PROC) -> int | None:
    """Field 22 of /proc/<pid>/stat: start time in clock ticks since boot."""
    try:
        stat = (proc / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces and parens; split after the last ')'
    return int(stat.rsplit(")", 1)[1].split()[19])


def is_alive(pid, start, boot, proc: Path = PROC) -> bool:
    if not pid or start is None or boot != boot_id(proc):
        return False
    return start_time(pid, proc) == start


def status(session, *, now: datetime | None = None, waking: bool = False, proc: Path = PROC) -> str:
    """One of: live, stalled, starting, dead. Always from the process: being parked is
    a separate flag and never hides whether a session is running."""
    now = now or datetime.now(timezone.utc)
    if is_alive(session["claude_pid"], session["claude_start"], session["boot_id"], proc):
        beat = session["heartbeat_at"]
        if not waking and beat and (now - datetime.fromisoformat(beat)).total_seconds() > STALLED_AFTER:
            return "stalled"
        return "live"
    launched = session["launched_at"]
    registered_this_launch = session["claude_pid"] and session["heartbeat_at"] and launched \
        and session["heartbeat_at"] >= launched
    if launched and not registered_this_launch \
            and (now - datetime.fromisoformat(launched)).total_seconds() < STARTING_GRACE:
        return "starting"
    return "dead"


class WakeDetector:
    """Notices the machine waking from sleep: wall time jumps, monotonic time does not."""

    def __init__(self, clock=time.time, mono=time.monotonic):
        self.clock, self.mono = clock, mono
        self.last = (clock(), mono())
        self.grace_until = 0.0

    def tick(self) -> bool:
        """Returns True while inside the post-wake grace period."""
        wall, mono = self.clock(), self.mono()
        if (wall - self.last[0]) - (mono - self.last[1]) > CLOCK_JUMP:
            self.grace_until = mono + WAKE_GRACE
        self.last = (wall, mono)
        return mono < self.grace_until
