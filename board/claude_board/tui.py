"""The board itself: a Textual app. It only reads and writes the database."""

import math
import os
import sqlite3

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from . import launch, liveness
from .store import Store

MATRIX = "#00ff41"
SHIMMER = ["#ff2a6d", "#ff7b00", "#ffd300", "#05d9e8", "#7b61ff", "#d300c5"]
STATUS_STYLE = {"live": "bold #00ff41", "stalled": "bold #ffd300", "starting": "#05d9e8",
                "dead": "bold #ff2a6d", "ending": "bold #d300c5", "parking": "bold #d300c5"}
TITLE = " ▓▒░ CLAUDE·BOARD ░▒▓ "
RUNNING = ("live", "stalled", "starting")
PENDING = {"end": "ending", "park": "parking"}


def cylon(frame: int, width: int = 8) -> Text:
    """A red eye sweeping back and forth with a fading tail."""
    pos = round((math.sin(frame / 4) + 1) / 2 * (width - 1))
    t = Text()
    for i in range(width):
        d = abs(i - pos)
        t.append("█" if d == 0 else "▓" if d == 1 else "░" if d == 2 else " ",
                 style="#ff0000" if d == 0 else "#aa0000" if d == 1 else "#550000")
    return t


def shimmer(frame: int) -> Text:
    t = Text()
    for i, ch in enumerate(TITLE):
        t.append(ch, style=f"bold {SHIMMER[(i + frame) // 2 % len(SHIMMER)]}")
    return t


def short(sid: str) -> str:
    return sid[:6]


class NewSession(ModalScreen):
    """Name and ticket are optional; the brief becomes the first prompt."""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("NEW SESSION", classes="dialog-title")
            yield Input(value=os.getcwd(), placeholder="working directory", id="cwd")
            yield Input(placeholder="name (optional)", id="name")
            yield Input(placeholder="ticket: #42, owner/repo#42 or ABC-123 (optional)", id="ticket")
            yield TextArea(id="brief")
            with Horizontal(classes="buttons"):
                yield Button("Launch", variant="success", id="launch")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#launch")
    def launch(self) -> None:
        cwd = self.query_one("#cwd", Input).value.strip()
        if not os.path.isdir(cwd):
            self.notify(f"no such directory: {cwd}", severity="error")
            return
        self.dismiss({"cwd": cwd, "name": self.query_one("#name", Input).value.strip(),
                      "ticket": self.query_one("#ticket", Input).value.strip(),
                      "brief": self.query_one("#brief", TextArea).text.strip()})

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen):
    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt)
            with Horizontal(classes="buttons"):
                yield Button("[Y]es", variant="error", id="yes")
                yield Button("[N]o", id="no")

    def on_key(self, event) -> None:
        if event.key in ("y", "n"):
            self.dismiss(event.key == "y")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class Choice(ModalScreen):
    """A pending request: cancel it, force it, or leave it be."""

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt)
            with Horizontal(classes="buttons"):
                yield Button("[C]ancel request", variant="success", id="cancel")
                yield Button("[F]orce", variant="error", id="force")
                yield Button("Leave it [Esc]", id="leave")

    def on_key(self, event) -> None:
        keys = {"c": "cancel", "f": "force", "escape": "leave"}
        if event.key in keys:
            event.stop()
            self.dismiss(keys[event.key])

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


class BoardApp(App):
    CSS = f"""
    Screen {{ background: #0a0a12; }}
    #title {{ height: 1; background: #12122a; content-align: center middle; }}
    .panel {{ background: #000000; color: {MATRIX}; border: round #7b61ff; }}
    .panel:focus-within {{ border: round #ff2a6d; }}
    DataTable {{ background: #000000; color: {MATRIX}; }}
    DataTable > .datatable--header {{ background: #12122a; color: #05d9e8; text-style: bold; }}
    DataTable > .datatable--cursor {{ background: #003b0f; color: #ffffff; }}
    #sessions-pane {{ width: 34; }}
    #items-pane {{ width: 1fr; }}
    #detail-pane {{ width: 2fr; }}
    #detail {{ height: 1fr; background: #000000; color: {MATRIX}; }}
    #answer {{ height: 8; background: #000000; color: {MATRIX}; border: round #05d9e8; }}
    #answer-hint {{ color: #777777; height: 1; }}
    Markdown {{ background: #000000; color: {MATRIX}; }}
    #session-buttons {{ height: 3; }}
    #dialog {{ width: 80; height: auto; padding: 1 2; background: #000000; color: {MATRIX};
               border: thick #ff2a6d; }}
    .dialog-title {{ color: #ffd300; text-style: bold; }}
    #dialog TextArea {{ height: 8; }}
    .buttons {{ height: 3; }}
    NewSession, Confirm, Choice {{ align: center middle; }}
    """

    BINDINGS = [
        Binding("ctrl+s", "send", "Send"),
        Binding("ctrl+enter", "send", "Send", show=False),
        Binding("n", "new_session", "New session"),
        Binding("escape", "clear_filter", "All sessions"),
        Binding("i", "show_tab('inbox')", "Inbox"),
        Binding("s", "show_tab('sessions')", "Sessions"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, store: Store | None = None):
        super().__init__()
        self.store = store or Store()
        self.wake = liveness.WakeDetector()
        self.waking = False
        self.frame = 0
        self.filter_sid: str | None = None
        self.selected: tuple[str, str] | None = None   # (session id, item ref)
        self.statuses: dict[str, str] = {}
        self.sessions = []

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        with TabbedContent(initial="inbox"):
            with TabPane("Inbox", id="inbox"):
                with Horizontal():
                    with Vertical(id="sessions-pane", classes="panel"):
                        yield DataTable(id="session-list", cursor_type="row")
                    with Vertical(id="items-pane", classes="panel"):
                        yield DataTable(id="items", cursor_type="row")
                    with Vertical(id="detail-pane", classes="panel"):
                        yield Markdown("Select an item.", id="detail")
                        yield TextArea(id="answer")
                        yield Label("Ctrl+S sends to the session (Ctrl+Enter if your terminal reports it)",
                                    id="answer-hint")
            with TabPane("Sessions", id="sessions"):
                with Vertical(classes="panel"):
                    yield DataTable(id="session-table", cursor_type="row")
                    with Horizontal(id="session-buttons"):
                        yield Button("New session", id="new", variant="success")
                        yield Button("Restore", id="restore")
                        yield Button("Restore all", id="restore-all", variant="warning")
                        yield Button("Park / unpark", id="park")
                        yield Button("End", id="end", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        # held, not queried: timers fire while a dialog is on top and during shutdown
        self.title_bar = self.query_one("#title", Static)
        self.items_table = self.query_one("#items", DataTable)
        self.detail = self.query_one("#detail", Markdown)
        self.answer = self.query_one("#answer", TextArea)
        self.tables = {"#session-list": self.query_one("#session-list", DataTable),
                       "#session-table": self.query_one("#session-table", DataTable)}
        self.eye_cols = {
            "#session-list": self.tables["#session-list"].add_columns("", "session", "?", "")[-1],
            "#session-table": self.tables["#session-table"].add_columns(
                "status", "name", "ticket", "dir", "open Q", "running", "")[-1],
        }
        self.items_table.add_columns("session", "ref", "status", "title")
        self.set_interval(0.1, self.animate)
        self.set_interval(1.0, self.refresh_data)
        self.refresh_data()

    # periodic work

    def animate(self) -> None:
        self.frame += 1
        self.title_bar.update(shimmer(self.frame))
        if self.frame % 2 == 0:
            self.sweep_eyes()

    def refresh_data(self) -> None:
        self.waking = self.wake.tick()
        self.sessions = self.store.sessions()
        # liveness only: the board never deletes or parks anything by itself
        self.statuses = {s["id"]: liveness.status(s, waking=self.waking) for s in self.sessions}
        self.paint_sessions()
        self.paint_items()

    def running(self, sid: str) -> bool:
        return self.statuses.get(sid) in RUNNING

    @staticmethod
    def pending(s) -> str | None:
        """The request (end or park) a session has been asked to act on, if any."""
        return next((what for what in PENDING if s[f"{what}_requested_at"]), None)

    def shown_status(self, s) -> str:
        st = self.statuses.get(s["id"], "dead")
        what = self.pending(s)
        return PENDING[what] if what and st in RUNNING else st

    def busy(self, s) -> bool:
        return bool(s["running"]) and self.statuses.get(s["id"]) in ("live", "stalled")

    def sweep_eyes(self) -> None:
        """Move the Cylon eye on busy rows without rebuilding the tables."""
        for table_id, col in self.eye_cols.items():
            table = self.tables[table_id]
            for s in self.sessions:
                if self.busy(s) and s["id"] in table.rows:
                    table.update_cell(s["id"], col, cylon(self.frame))

    def paint_sessions(self) -> None:
        for table_id, compact in (("#session-list", True), ("#session-table", False)):
            table = self.tables[table_id]
            keep = table.cursor_row
            table.clear()
            for s in self.sessions:
                st = self.shown_status(s)
                if compact and s["parked"]:
                    continue
                busy = cylon(self.frame) if self.busy(s) else Text("")
                dot = Text("●", style=STATUS_STYLE[st])
                label = Text(st, style=STATUS_STYLE[st])
                if s["parked"]:
                    label.append(" · parked", style="#777777")
                name = s["name"] or os.path.basename(s["cwd"]) or short(s["id"])
                if compact:
                    q = Text(str(s["open_questions"]), style="bold #ffd300 blink") if s["open_questions"] else ""
                    table.add_row(dot, name, q, busy, key=s["id"])
                else:
                    table.add_row(label, name, s["ticket"], s["cwd"],
                                  str(s["open_questions"]), str(s["running"]), busy, key=s["id"])
            if table.row_count:
                table.move_cursor(row=min(keep, table.row_count - 1), animate=False)

    def paint_items(self) -> None:
        table = self.items_table
        keep = table.cursor_row
        table.clear()
        names = {s["id"]: s["name"] or short(s["id"]) for s in self.sessions}
        for it in self.store.items(self.filter_sid, include_closed=False):
            style = "bold #ffd300" if it["status"] == "open" else "bold #ff2a6d" \
                if it["status"] in ("blocked", "waiting") else MATRIX
            table.add_row(names.get(it["session_id"], "")[:14], it["ref"], Text(it["status"], style=style),
                          it["title"], key=f"{it['session_id']}|{it['ref']}")
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1), animate=False)
        self.paint_detail()

    def paint_detail(self) -> None:
        if not self.selected:
            return
        sid, ref = self.selected
        item = self.store.item(sid, ref)
        if item is None:
            self.detail.update("_gone_")
            return
        lines = [f"## {ref} · {item['title']}", f"`{item['kind']}` · **{item['status']}** · "
                 f"updated {item['updated_at']}", "", item["body"] or "_no detail_", ""]
        for m in self.store.thread(sid, ref):
            who = "you" if m["author"] == "person" else "claude"
            lines += [f"**{who}** · {m['created_at']}", "", m["body"], ""]
        text = "\n".join(lines)
        if text != getattr(self, "_detail_text", None):
            self._detail_text = text
            self.detail.update(text)

    # selection

    @on(DataTable.RowSelected, "#session-list")
    def pick_session(self, event: DataTable.RowSelected) -> None:
        self.filter_sid = event.row_key.value
        self.paint_items()

    @on(DataTable.RowHighlighted, "#items")
    def pick_item(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value:
            sid, ref = event.row_key.value.split("|")
            if self.selected != (sid, ref):
                self.selected = (sid, ref)
                self.paint_detail()

    def action_clear_filter(self) -> None:
        self.filter_sid = None
        self.paint_items()

    def action_show_tab(self, tab: str) -> None:
        if not isinstance(self.focused, (TextArea, Input)):
            self.query_one(TabbedContent).active = tab

    def action_send(self) -> None:
        box = self.answer
        text = box.text.strip()
        if not text or not self.selected:
            self.notify("pick an item and type something first", severity="warning")
            return
        sid, ref = self.selected
        try:
            if self.store.session(sid) is None:
                raise sqlite3.IntegrityError
            self.store.send(sid, text, ref)
        except sqlite3.IntegrityError:   # the session ended, possibly between the check and the write
            self.notify(f"{ref}'s session has ended", severity="warning")
            self.selected = None
            self.paint_items()
            return
        box.text = ""
        self.notify(f"sent to {ref}")
        self.paint_items()

    # sessions page

    def current_session(self) -> str | None:
        table = self.tables["#session-table"]
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value

    def action_new_session(self) -> None:
        if isinstance(self.focused, (TextArea, Input)):
            return
        self.push_screen(NewSession(), self.launch_new)

    def launch_new(self, form) -> None:
        if not form:
            return
        sid = self.store.create_session(**form)
        self.open_tab(sid)

    def open_tab(self, sid: str) -> bool:
        try:
            launch.open_tab(self.store, sid)
        except Exception as e:   # already running, wt.exe missing...
            self.notify(str(e), severity="error")
            return False
        return True

    @on(Button.Pressed, "#new")
    def new_pressed(self) -> None:
        self.push_screen(NewSession(), self.launch_new)

    def label(self, sid: str) -> str:
        s = self.store.session(sid)
        return f"{s['name'] or os.path.basename(s['cwd'])} ({short(sid)}, {s['cwd']})"

    @on(Button.Pressed, "#restore")
    def restore_pressed(self) -> None:
        sid = self.current_session()
        if not sid:
            return
        if self.statuses.get(sid) != "dead":
            self.notify("only a dead session can be restored", severity="warning")
            return
        if self.open_tab(sid):
            self.store.set_parked(sid, False)

    @on(Button.Pressed, "#restore-all")
    def restore_all_pressed(self) -> None:
        dead = [s["id"] for s in self.sessions if self.statuses.get(s["id"]) == "dead" and not s["parked"]]
        if not dead:
            self.notify("nothing to restore")
            return

        def go(yes: bool) -> None:
            if yes:
                n = sum(self.open_tab(sid) for sid in dead)
                self.notify(f"restoring {n} session(s)")
        self.push_screen(Confirm(f"Restore {len(dead)} dead session(s) in new tabs?"), go)

    @on(Button.Pressed, "#park")
    def park_pressed(self) -> None:
        sid = self.current_session()
        if not sid:
            return
        if self.store.session(sid)["parked"]:
            self.store.set_parked(sid, False)
            self.refresh_data()
            return
        self.lifecycle(sid, "park",
                       ask=f"Ask {self.label(sid)} to park? It brings its items up to date, then parks.",
                       act=f"Park {self.label(sid)}? It drops off the inbox and Restore all.")

    @on(Button.Pressed, "#end")
    def end_pressed(self) -> None:
        sid = self.current_session()
        if sid:
            self.lifecycle(sid, "end",
                           ask=f"Ask {self.label(sid)} to end? Claude saves memory, then deletes its board data.",
                           act=f"End {self.label(sid)} and delete its board data?")

    def lifecycle(self, sid: str, what: str, ask: str, act: str) -> None:
        """Park or End. A running session is only asked; a dead one is acted on at once."""
        if not self.running(sid):
            self.push_screen(Confirm(act), lambda yes: yes and self.force(sid, what))
            return
        pending = self.pending(self.store.session(sid))
        if pending is None:
            def go(yes: bool) -> None:
                if yes:
                    self.store.request(sid, what)
                    self.notify(f"asked the session to {what}")
                    self.refresh_data()
            self.push_screen(Confirm(ask), go)
        elif pending != what:
            self.notify(f"the session has already been asked to {pending}: cancel that first",
                        severity="warning")
        else:
            self.push_screen(Choice(f"{self.label(sid)} has been asked to {what} and hasn't yet. "
                                    "Cancel the request, or force it?"),
                             lambda choice: self.settle(sid, what, choice))

    def settle(self, sid: str, what: str, choice: str) -> None:
        if choice == "cancel":
            self.store.cancel_request(sid, what)
            self.notify(f"{what} request cancelled")
            self.refresh_data()
        elif choice == "force":
            warning = ("Deletes its board data now, without the memory save." if what == "end"
                       else "Parks it now; it is still running.")
            self.push_screen(Confirm(f"Force {what} {self.label(sid)}? {warning}"),
                             lambda yes: yes and self.force(sid, what))

    def force(self, sid: str, what: str) -> None:
        if what == "end":
            self.store.end(sid)
        else:
            self.store.set_parked(sid, True)
        self.refresh_data()
