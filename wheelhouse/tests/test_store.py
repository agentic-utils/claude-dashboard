import pytest

from claude_wheelhouse.store import Store


def test_pragmas_make_every_commit_durable(store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2   # FULL


def test_refuses_windows_mount(monkeypatch):
    monkeypatch.setenv("WHEELHOUSE_DB", "/mnt/c/wheelhouse.db")
    with pytest.raises(ValueError, match="/mnt/"):
        Store()


@pytest.mark.parametrize("posts, expected, desc", [
    ([("task", None)], [("T1", "todo")], "task starts todo"),
    ([("question", None)], [("Q1", "open")], "question starts open"),
    ([("agent", None)], [("A1", "running")], "agent starts running"),
    ([("task", None), ("task", "running"), ("question", None)],
     [("T1", "todo"), ("T2", "running"), ("Q1", "open")], "refs count per kind"),
])
def test_post_item_refs_and_statuses(store, sid, posts, expected, desc):
    refs = [store.post_item(sid, kind, "title", "body", status) for kind, status in posts]
    got = [(r, store.item(sid, r)["status"]) for r in refs]
    assert got == expected, desc


@pytest.mark.parametrize("call, error, desc", [
    (lambda s, sid: s.post_item(sid, "bug", "x"), ValueError, "unknown kind"),
    (lambda s, sid: s.post_item(sid, "task", "x", status="answered"), ValueError, "question status on a task"),
    (lambda s, sid: s.update_item(sid, "T9", status="done"), KeyError, "missing ref"),
])
def test_rejects_bad_writes(store, sid, call, error, desc):
    with pytest.raises(error):
        call(store, sid)


def test_answer_marks_question_answered_and_waits_for_delivery(store, sid):
    q = store.post_item(sid, "question", "which db?", "full detail")
    store.send(sid, "postgres", q)
    assert store.item(sid, q)["status"] == "answered"
    assert [m["body"] for m in store.pending(sid)] == ["postgres"]
    assert [m["body"] for m in store.take_pending(sid)] == ["postgres"]
    assert store.pending(sid) == [] and store.take_pending(sid) == []
    assert [m["body"] for m in store.thread(sid, q)] == ["postgres"]


def test_update_note_lands_in_thread_already_delivered(store, sid):
    t = store.post_item(sid, "task", "build it")
    store.update_item(sid, t, status="running", note="halfway")
    assert store.item(sid, t)["status"] == "running"
    assert store.pending(sid) == []
    assert [(m["author"], m["body"]) for m in store.thread(sid, t)] == [("claude", "halfway")]


def test_inbox_puts_open_questions_first_and_hides_parked(store, sid, tmp_path):
    store.post_item(sid, "task", "a task", status="running")
    store.post_item(sid, "task", "blocked", status="blocked")
    store.post_item(sid, "question", "a question")
    other = store.create_session(str(tmp_path), name="parked one")
    store.post_item(other, "question", "hidden")
    store.set_parked(other, True)
    assert [i["title"] for i in store.items()] == ["a question", "blocked", "a task"]
    assert [i["title"] for i in store.items(other)] == ["hidden"], "a session still sees its own items"


def test_end_deletes_everything(store, sid):
    q = store.post_item(sid, "question", "q")
    store.send(sid, "a", q)
    store.end(sid)
    assert store.session(sid) is None
    assert store.db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_one_store_survives_parallel_writers(store, sid):
    """The MCP server runs tool calls in worker threads on one Store (review #2)."""
    import threading
    errors = []

    def post():
        for _ in range(25):
            try:
                store.post_item(sid, "task", "t")
            except Exception as e:
                errors.append(e)
    threads = [threading.Thread(target=post) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(int(r["ref"][1:]) for r in store.items(sid)) == list(range(1, 151))


def test_take_pending_delivers_each_message_once(db_file, store, sid):
    """The monitor and get_input() may read at the same moment (review #10)."""
    import threading
    for i in range(200):
        store.send(sid, f"m{i}")
    seen = []

    def take():
        reader = Store(db_file)
        while msgs := reader.take_pending(sid):
            seen.extend(m["id"] for m in msgs)
    threads = [threading.Thread(target=take) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == sorted(set(seen)) and len(seen) == 200
