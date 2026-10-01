import pytest
from textual.widgets import DataTable, TextArea

from claude_board import launch, liveness
from claude_board.tui import BoardApp, Confirm


@pytest.mark.anyio
async def test_answer_reaches_the_session(store, sid):
    q = store.post_item(sid, "question", "which db?", "Postgres or SQLite for the cache?")
    store.post_item(sid, "task", "build", status="running")
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        items = app.query_one("#items", DataTable)
        assert items.row_count == 2
        assert items.get_row_at(0)[1] == q, "open question first"
        items.move_cursor(row=0)
        await pilot.pause()
        assert app.selected == (sid, q)
        app.query_one("#answer", TextArea).text = "SQLite, it's local"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert [m["body"] for m in store.pending(sid)] == ["SQLite, it's local"]
    assert store.item(sid, q)["status"] == "answered"


@pytest.mark.anyio
async def test_restore_all_only_launches_dead_sessions(store, sid, tmp_path, monkeypatch):
    parked = store.create_session(str(tmp_path), name="parked")
    store.set_parked(parked, True)
    launched = []
    monkeypatch.setattr(launch, "open_tab", lambda s, i: launched.append(i))
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("s")
        await pilot.click("#restore-all")
        await pilot.press("y")
        await pilot.pause()
    assert launched == [sid]


@pytest.mark.anyio
async def test_timers_keep_running_under_a_dialog(store, sid):
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        app.push_screen(Confirm("sure?"))
        await pilot.pause()
        app.animate()
        app.refresh_data()


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def press_end(app, pilot):
    await pilot.press("s")
    await pilot.pause()
    await pilot.click("#end")
    await pilot.pause()
    prompt = app.screen.prompt
    await pilot.press("y")
    await pilot.pause()
    return prompt


@pytest.mark.anyio
@pytest.mark.parametrize("state, kept, desc", [
    ("live", True, "a running session is asked to save memory and end itself (review #5)"),
    ("stalled", True, "so is a quiet one"),
    ("dead", False, "a dead session is deleted straight away"),
    ("parked", False, "so is a parked one"),
])
async def test_end(store, sid, monkeypatch, state, kept, desc):
    monkeypatch.setattr(liveness, "status", lambda s, **kw: state)
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        prompt = await press_end(app, pilot)
    assert "demo" in prompt, "the confirm names the session (review #7)"
    assert (store.session(sid) is not None) == kept, desc
    if kept:
        assert store.session(sid)["end_requested_at"], desc
        assert store.pending(sid) == [], "an end request is a flag, not a message (round-2 #1)"


@pytest.mark.anyio
async def test_sending_to_an_ended_session_does_not_crash(store, sid):
    """Review #6: the session row went away under the selected item."""
    q = store.post_item(sid, "question", "which db?")
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        assert app.selected == (sid, q)
        store.end(sid)
        app.query_one("#answer", TextArea).text = "too late"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert store.session(sid) is None


@pytest.mark.anyio
async def test_a_refused_restore_leaves_the_session_parked(store, sid, monkeypatch):
    """Review #8: unpark only once the launch has gone through."""
    store.set_parked(sid, True)

    def refuse(s, i):
        raise RuntimeError("already running")
    monkeypatch.setattr(launch, "open_tab", refuse)
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.click("#restore")
        await pilot.pause()
    assert store.session(sid)["parked"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("state, kept, desc", [
    ("live", True, "a session still running keeps its rows while it saves memory"),
    ("dead", False, "one that died before acting on End is deleted (round-2 #1)"),
    ("parked", False, "so is a parked one"),
])
async def test_an_end_request_on_a_session_that_is_gone_deletes_it(store, sid, monkeypatch, state, kept, desc):
    store.request_end(sid)
    monkeypatch.setattr(liveness, "status", lambda s, **kw: state)
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
    assert (store.session(sid) is not None) == kept, desc


def test_a_relaunch_drops_a_leftover_end_request(store, sid):
    """Round-2 #1: a restored session must not be told to end itself."""
    store.request_end(sid)
    store.mark_launched(sid)
    assert store.session(sid)["end_requested_at"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("age, deleted, desc", [
    (10, False, "too soon: the board waits for the session to end itself"),
    (600, True, "after the timeout End offers a force end (round-2 #3)"),
])
async def test_force_end_once_the_session_stops_responding(store, sid, monkeypatch, age, deleted, desc):
    from datetime import datetime, timedelta, timezone
    then = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(timespec="seconds")
    store.db.execute("UPDATE sessions SET end_requested_at = ? WHERE id = ?", (then, sid))
    monkeypatch.setattr(liveness, "status", lambda s, **kw: "live")
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("s")
        await pilot.pause()
        assert "ending" in str(app.tables["#session-table"].get_row_at(0)[0]), "pending-end state shows"
        await pilot.click("#end")
        await pilot.pause()
        if deleted:
            assert "demo" in app.screen.prompt and "Force" in app.screen.prompt, desc
            await pilot.press("y")
            await pilot.pause()
    assert (store.session(sid) is None) == deleted, desc


@pytest.mark.anyio
async def test_a_send_racing_an_end_does_not_crash(store, sid, monkeypatch):
    """Round-2 #7: the session can end between the board's check and its write."""
    q = store.post_item(sid, "question", "which db?")
    app = BoardApp(store)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.query_one("#items", DataTable).move_cursor(row=0)
        await pilot.pause()
        store.end(sid)
        monkeypatch.setattr(store, "session", lambda s: {"id": s})   # the check still sees it
        app.query_one("#answer", TextArea).text = "too late"
        await pilot.press("ctrl+s")
        await pilot.pause()
