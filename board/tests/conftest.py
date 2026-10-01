import pytest

from claude_board.store import Store


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    monkeypatch.setenv("BOARD_DB", str(path))
    return path


@pytest.fixture
def store(db_file):
    return Store(db_file)


@pytest.fixture
def sid(store, tmp_path):
    return store.create_session(str(tmp_path), name="demo", ticket="#7", brief="do the thing")
