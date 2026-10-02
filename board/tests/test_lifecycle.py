"""Park and End: the board never deletes or hides a running session's data behind its back.

On a running session the buttons only ask; the session acts, or the person cancels or
forces. On a dead session they act straight away. Nothing is deleted automatically.
"""

import io
import threading

import pytest

from claude_board import launch, liveness, monitor
from claude_board.store import Store
from claude_board.tui import BoardApp


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_status(monkeypatch, state):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: state)


async def press(app, pilot, button, *keys):
    await pilot.press("s")
    await pilot.pause()
    await pilot.click(button)
    await pilot.pause()
    prompts = []
    for key in keys:
        prompts.append(getattr(app.screen, "prompt", ""))
        await pilot.press(key)
        await pilot.pause()
    return prompts


@pytest.mark.anyio
@pytest.mark.parametrize("button, state, parked, kept, want_parked, end_flag, park_flag, desc", [
    ("#end", "live", 0, True, 0, True, False, "End on a running session only asks it to end"),
    ("#end", "stalled", 0, True, 0, True, False, "so does End on a quiet one"),
    ("#end", "dead", 0, False, None, None, None, "End on a dead session deletes it"),
    ("#end", "live", 1, True, 1, True, False, "a parked flag doesn't make a running session deletable (round-3 #1)"),
    ("#end", "dead", 1, False, None, None, None, "a dead parked session is deleted"),
    ("#park", "live", 0, True, 0, False, True, "Park on a running session only asks it to park"),
    ("#park", "dead", 0, True, 1, False, False, "Park on a dead session parks it"),
])
async def test_buttons_ask_running_sessions_and_act_on_dead_ones(
        store, sid, monkeypatch, button, state, parked, kept, want_parked, end_flag, park_flag, desc):
    store.set_parked(sid, bool(parked))
    fake_status(monkeypatch, state)
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        prompts = await press(app, pilot, button, "y")
    assert "demo" in prompts[0], "the confirm names the session"
    s = store.session(sid)
    assert (s is not None) == kept, desc
    if kept:
        assert s["parked"] == want_parked, desc
        assert bool(s["end_requested_at"]) == end_flag, desc
        assert bool(s["park_requested_at"]) == park_flag, desc
        assert store.pending(sid) == [], "a request is a flag, not a message"


@pytest.mark.anyio
async def test_unpark_is_immediate(store, sid, monkeypatch):
    store.set_parked(sid, True)
    fake_status(monkeypatch, "dead")
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await press(app, pilot, "#park")
    assert store.session(sid)["parked"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("state, desc", [
    ("dead", "an end request left on a session that died"),
    ("live", "an end request on a running session"),
])
async def test_the_board_never_deletes_by_itself(store, sid, monkeypatch, state, desc):
    store.request_end(sid)
    store.set_parked(sid, True)
    fake_status(monkeypatch, state)
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.refresh_data()
    assert store.session(sid) is not None, desc


@pytest.mark.anyio
@pytest.mark.parametrize("button, flag, keys, kept, parked, notice, desc", [
    ("#end", "end", ["c"], True, 0, True, "Cancel clears the end request and tells the session"),
    ("#end", "end", ["f", "y"], False, None, False, "Force end deletes after a confirm"),
    ("#end", "end", ["f", "n"], True, 0, False, "Force end can be backed out of"),
    ("#end", "end", ["escape"], True, 0, False, "Escape leaves the request pending"),
    ("#park", "park", ["c"], True, 0, True, "Cancel clears the park request and tells the session"),
    ("#park", "park", ["f", "y"], True, 1, False, "Force park parks it after a confirm"),
])
async def test_a_pending_request_can_be_cancelled_or_forced(
        store, sid, monkeypatch, button, flag, keys, kept, parked, notice, desc):
    getattr(store, f"request_{flag}")(sid)
    monitor.poll_once(store, sid, io.StringIO())   # the session has been told
    fake_status(monkeypatch, "live")
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("s")
        await pilot.pause()
        label = "ending" if flag == "end" else "parking"
        assert label in str(app.tables["#session-table"].get_row_at(0)[0]), "pending state shows"
        prompts = await press(app, pilot, button, *keys)
    assert all("demo" in p for p in prompts), "every prompt names the session"
    s = store.session(sid)
    assert (s is not None) == kept, desc
    if kept:
        assert s["parked"] == parked, desc
        cancelled = keys == ["c"]
        assert (s[f"{flag}_requested_at"] is None) == (cancelled or parked == 1), desc
        assert bool(store.pending(sid)) == notice, desc


@pytest.mark.anyio
@pytest.mark.parametrize("state, parked, launched, desc", [
    ("dead", 0, True, "a dead session is restored"),
    ("dead", 1, True, "a dead parked session is restored (and unparked)"),
    ("live", 1, False, "a running parked session is not restored"),
])
async def test_restore_goes_by_liveness_not_the_parked_flag(store, sid, monkeypatch, state, parked, launched, desc):
    store.set_parked(sid, bool(parked))
    fake_status(monkeypatch, state)
    calls = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: calls.append(i))
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await press(app, pilot, "#restore")
    assert bool(calls) == launched, desc
    if launched:
        assert store.session(sid)["parked"] == 0, desc


@pytest.mark.anyio
async def test_restore_all_skips_parked_sessions(store, sid, tmp_path, monkeypatch):
    parked = store.create_session(str(tmp_path), name="parked")
    store.set_parked(parked, True)
    fake_status(monkeypatch, "dead")
    calls = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: calls.append(i))
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await press(app, pilot, "#restore-all", "y")
    assert calls == [sid]


@pytest.mark.parametrize("steps, expected, desc", [
    (["end"], ["end_session"], "an end request is passed on"),
    (["park"], ["park_session"], "so is a park request"),
    (["end", "poll"], ["end_session"], "once"),
    (["end", "cancel", "end"], ["end_session", "cancelled", "end_session"],
     "a new request after a cancel is passed on again"),
])
def test_monitor_passes_requests_on(store, sid, steps, expected, desc):
    out = io.StringIO()   # what was passed on lives in the database, not the monitor
    for step in steps:
        if step == "end":
            store.request_end(sid)
        elif step == "park":
            store.request_park(sid)
        elif step == "cancel":
            store.cancel_request(sid, "end")
        monitor.poll_once(store, sid, out)
    got = [w for line in out.getvalue().splitlines()
           for w in ("end_session", "park_session", "cancelled") if w in line]
    assert got == expected, desc


def test_park_session_clears_the_park_request(store, sid):
    from claude_board import mcp_server
    store.request_park(sid)
    mcp_server._store, mcp_server._sid = store, sid
    mcp_server.park_session()
    s = store.session(sid)
    assert s["parked"] == 1 and s["park_requested_at"] is None


def test_a_relaunch_drops_leftover_requests(store, sid):
    store.request_end(sid)
    store.request_park(sid)
    store.mark_launched(sid)
    s = store.session(sid)
    assert s["end_requested_at"] is None and s["park_requested_at"] is None


def test_confirm_only_touches_its_own_claim(store, sid):
    """Round-3 #3: a claim retaken by someone else must not be confirmed by the old claimer."""
    store.send(sid, "hello")
    old = store.claim(sid)
    store.db.execute("UPDATE messages SET claimed_at = '2000-01-01T00:00:00+00:00'")
    new = store.claim(sid)   # retaken as stale
    store.confirm(old)
    assert store.pending(sid) != [], "the old claimer's confirm is ignored"
    store.confirm(new)
    assert store.pending(sid) == []


def test_a_thread_leaves_out_messages_the_monitor_is_printing(store, sid):
    """Round-3 #4: get_input(ref) mustn't show what the monitor is about to deliver."""
    from claude_board import mcp_server
    q = store.post_item(sid, "question", "db?")
    store.send(sid, "postgres", q)
    store.claim(sid)   # the monitor has it, mid-print
    mcp_server._store, mcp_server._sid = store, sid
    assert "postgres" not in mcp_server.get_input(q)


def old_schema_db(path):
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")   # every earlier release created it in WAL mode
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', ticket TEXT NOT NULL DEFAULT '',
            brief TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL, parked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, launched_at TEXT, claude_pid INTEGER, claude_start INTEGER,
            boot_id TEXT, heartbeat_at TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, item_ref TEXT,
            author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT);
    """)
    db.close()


def test_concurrent_first_start_on_an_old_database(tmp_path, monkeypatch):
    """Round-3 #5: two processes upgrading the same database at once must both start."""
    import sqlite3
    real_connect = sqlite3.connect
    for n in range(10):
        path = tmp_path / f"old{n}.db"
        old_schema_db(path)
        gate = threading.Barrier(4)
        errors = []

        def connect(*a, gate=gate, **kw):
            db = real_connect(*a, **kw)
            gate.wait()   # every store reads the old schema before any of them alters it
            return db
        monkeypatch.setattr(sqlite3, "connect", connect)

        def start(path=path, errors=errors):
            try:
                Store(path)
            except Exception as e:   # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=start) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        monkeypatch.setattr(sqlite3, "connect", real_connect)
        assert errors == [], f"attempt {n}: {errors}"


# round 4: the session can vanish (in-session /wheelhouse end, or another board) while a dialog is open

def notices(app, monkeypatch):
    seen = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: seen.append(msg))
    return seen


@pytest.mark.anyio
@pytest.mark.parametrize("state, request_first, button, keys, desc", [
    ("live", "end", "#end", ["c"], "Cancel after the session ended itself (round-4 #1)"),
    ("live", "end", "#end", ["f"], "Force after the session ended itself (round-4 #2)"),
    ("live", "end", "#end", ["f", "y"], "Force confirmed after the session ended itself"),
    ("live", None, "#end", ["y"], "the ask confirmed after the session ended itself"),
    ("live", None, "#park", ["y"], "the park ask confirmed after the session ended itself"),
    ("dead", None, "#end", ["y"], "the dead-session confirm after the row went"),
])
async def test_a_dialog_outliving_its_session_does_not_crash(
        store, sid, monkeypatch, state, request_first, button, keys, desc):
    if request_first:
        store.request(sid, request_first)
    fake_status(monkeypatch, state)
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        seen = notices(app, monkeypatch)
        await press(app, pilot, button)   # the dialog is open
        store.end(sid)                    # the session ends itself meanwhile
        for key in keys:
            await pilot.press(key)
            await pilot.pause()
        assert app.is_running, desc
    assert app.return_code in (None, 0), desc
    assert store.session(sid) is None, desc
    assert any("no longer exists" in m for m in seen), desc


@pytest.mark.anyio
@pytest.mark.parametrize("button, desc", [
    ("#park", "Park pressed before the table repaints (round-4 #2)"),
    ("#end", "End pressed before the table repaints (round-4 #2)"),
])
async def test_a_button_on_a_vanished_row_does_not_crash(store, sid, monkeypatch, button, desc):
    fake_status(monkeypatch, "live")
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        seen = notices(app, monkeypatch)
        await pilot.press("s")
        await pilot.pause()
        stale = store.sessions()
        store.end(sid)
        # the row is still on screen: keep the 1 s repaint from removing it before the click
        monkeypatch.setattr(store, "sessions", lambda: stale)
        await pilot.click(button)
        await pilot.pause()
        assert app.is_running, desc
    assert app.return_code in (None, 0), desc
    assert any("no longer exists" in m for m in seen), desc


@pytest.mark.anyio
async def test_the_dead_path_rechecks_liveness_at_confirm(store, sid, monkeypatch):
    """Round-4 #4: the session came back while the confirm was open: ask, don't act."""
    fake_status(monkeypatch, "dead")
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await press(app, pilot, "#end")
        fake_status(monkeypatch, "live")
        await pilot.press("y")
        await pilot.pause()
    s = store.session(sid)
    assert s is not None, "a running session is not deleted"
    assert s["end_requested_at"] is None, "and not asked either: the person confirmed a different action"


@pytest.mark.parametrize("told, notice, desc", [
    (False, False, "a request the session never saw is cancelled quietly (round-4 #5)"),
    (True, True, "a request the session was told about gets a carry-on notice"),
])
def test_cancel_only_tells_a_session_that_was_told(store, sid, told, notice, desc):
    store.request(sid, "end")
    if told:
        monitor.poll_once(store, sid, io.StringIO())
    store.cancel_request(sid, "end")
    assert bool(store.pending(sid)) == notice, desc


@pytest.mark.parametrize("call", [
    lambda s, i: s.request(i, "end"),
    lambda s, i: s.cancel_request(i, "end"),
    lambda s, i: s.set_parked(i, True),
    lambda s, i: s.send(i, "hello"),
    lambda s, i: s.post_item(i, "task", "x"),
    lambda s, i: s.update_item(i, "T1", status="done"),
], ids=["request", "cancel", "park", "send", "post", "update"])
def test_writes_to_a_vanished_session_raise_session_gone(store, sid, call):
    from claude_board.store import SessionGone
    store.post_item(sid, "task", "x")
    store.end(sid)
    with pytest.raises(SessionGone):
        call(store, sid)


@pytest.mark.parametrize("tool, args", [
    ("post_item", ("task", "x")),
    ("update_item", ("T1",)),
    ("get_input", ()),
    ("get_input", ("T1",)),
    ("list_items", ()),
    ("park_session", ()),
    ("end_session", ()),
])
def test_board_tools_say_the_session_was_force_ended(store, sid, tool, args):
    """Round-4 #3: a clear answer instead of a raw IntegrityError."""
    from claude_board import mcp_server
    store.post_item(sid, "task", "x")
    store.end(sid)
    mcp_server._store, mcp_server._sid = store, sid
    assert "force-ended on the board" in getattr(mcp_server, tool)(*args)


def test_the_monitor_says_so_once_and_stops(store, sid):
    out = io.StringIO()
    store.end(sid)
    assert monitor.poll_once(store, sid, out) is None
    assert out.getvalue().count("force-ended on the board") == 1
