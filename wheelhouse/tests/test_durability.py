"""Kill a writer mid-run: every row it reported as committed must still be there."""

import os
import signal
import subprocess
import sys
import textwrap

from claude_wheelhouse.store import Store

WRITER = textwrap.dedent("""
    import sys
    from claude_wheelhouse.store import Store
    s = Store(sys.argv[1])
    sid = s.create_session("/tmp")
    print(sid, flush=True)
    i = 0
    while True:
        i += 1
        s.post_item(sid, "task", f"row {i}")
        print(i, flush=True)   # only after the commit returned
""")


def test_committed_rows_survive_sigkill(tmp_path):
    db = tmp_path / "wheelhouse.db"
    proc = subprocess.Popen([sys.executable, "-c", WRITER, str(db)], stdout=subprocess.PIPE, text=True)
    sid = proc.stdout.readline().strip()
    committed = 0
    while committed < 50:
        committed = int(proc.stdout.readline())
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()

    rows = Store(db).db.execute("SELECT count(*) FROM items WHERE session_id = ?", (sid,)).fetchone()[0]
    assert rows >= committed
