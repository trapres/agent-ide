"""Three-pane native terminal and recording event cards."""
import curses
import json
import os
from pathlib import Path
import select
import signal
import unicodedata

from .input import InputRouter
from .layout import Rect, layout
from .pty_process import PtyProcess
from .terminal import Terminal, cell_width


def safe_text(value):
    # Tool output must not execute terminal sequences in review panes.
    return "".join("    " if c == "\t" else "?" if unicodedata.category(c) == "Cc" else c
                   for c in str(value))


def clip(text, width):
    result, used = [], 0
    for char in safe_text(text):
        w = cell_width(char)
        if used + w > width:
            break
        result.append(char)
        used += w
    return "".join(result)


class Harness:
    def __init__(self, command, workspace, events, side="left", agent_fraction=.5, activity_fraction=.5,
                 watch=False, watch_backend="native", recording=None, policy=None):
        self.command, self.workspace, self.events = command, workspace, Path(events)
        self.side = side
        self.agent_fraction, self.activity_fraction = agent_fraction, activity_fraction
        self.focus = "agent"
        self.maximized = False
        self.actions = []
        self.seen = set()
        self.selected = 0
        self.follow = True
        self.scroll = 0
        self.router = InputRouter()
        self.child = None
        self.watch_enabled, self.watch_backend = watch, watch_backend
        self.watcher = None
        self.recording = recording
        self.recorder = None
        from .policy import CapturePolicy
        self.policy = policy or CapturePolicy()
        self.policy_view = bool(recording)
        self.pairs = {}
        self.notice = "pyte terminal | provider coverage unverified"
        self.running = True

    def geometry(self, screen):
        rows, columns = screen.getmaxyx()
        return layout(rows, columns, self.side, self.agent_fraction,
                      self.activity_fraction, self.focus, self.maximized)

    def collect(self):
        fresh = []
        for file in ([] if self.recorder else self.events.glob("*.json")):
            if file.name in self.seen:
                continue
            self.seen.add(file.name)
            try:
                record = json.loads(file.read_text())
                if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                    raise ValueError("invalid hook event")
                fresh.append(record)
            except (ValueError, OSError):
                self.notice = "Unreadable hook event; coverage incomplete"
        if self.recorder:
            fresh.extend(self.recorder.drain())
            self.notice = self.recorder.notice
        if self.watcher:
            fresh.extend(self.watcher.drain())
            if self.watcher.dropped:
                self.notice = "watcher dropped %s observations; coverage incomplete" % self.watcher.dropped
        for record in sorted(fresh, key=lambda r: r.get("received_ns", 0)):
            payload = record.get("payload", {})
            call = payload.get("tool_use_id")
            # Only merge when both session and call identity are present.
            key = (payload.get("session_id"), call) if call else None
            existing = next((a for a in self.actions if key and a.get("key") == key), None)
            if existing:
                existing["payload"].update(payload)
            else:
                self.actions.append(dict(record, key=key))
                if self.follow:
                    self.selected = len(self.actions) - 1

    def handle(self, kind, token):
        if kind == "command":
            key = token.decode("ascii", "ignore")
            if key in ("a", "l", "v"):
                self.focus = {"a": "agent", "l": "activity", "v": "visualization"}[key]
            elif key == "\t":
                panes = ["agent", "activity", "visualization"]
                self.focus = panes[(panes.index(self.focus) + 1) % 3]
            elif key == "m":
                self.side = "right" if self.side == "left" else "left"
            elif key == "z":
                self.maximized = not self.maximized
            elif key == "b":
                self.terminal.scroll(max(1, self.terminal.rows // 2))
            elif key == "n":
                self.terminal.scroll(-1000)
            elif key in ("+", "-", "[", "]"):
                if key in ("+", "-"):
                    self.agent_fraction = min(.7, max(.3, self.agent_fraction + (.05 if key == "+" else -.05)))
                else:
                    self.activity_fraction = min(.7, max(.3, self.activity_fraction + (.05 if key == "]" else -.05)))
            elif key == "p":
                self.policy_view = not self.policy_view
                self.focus = "visualization"
                self.scroll = 0
            elif key == "q":
                self.running = False
            else:
                self.notice = "Prefix: a/l/v focus | Tab cycle | m mirror | z maximize | p policy | +/- width | [/] height | q stop"
            return
        if kind == "literal":
            self.child.send(token)
            return
        if self.focus == "agent":
            if kind in ("paste-start", "paste-end") and not self.terminal.bracketed_paste:
                return
            if kind == "key" and self.terminal.application_cursor:
                token = {b"\x1b[A": b"\x1bOA", b"\x1b[B": b"\x1bOB",
                         b"\x1b[C": b"\x1bOC", b"\x1b[D": b"\x1bOD"}.get(token, token)
            self.child.send(token)
        elif kind == "key":
            if self.focus == "activity":
                self.policy_view = False
                if token in (b"j", b"\x1b[B"):
                    self.selected = min(len(self.actions) - 1, self.selected + 1) if self.actions else 0
                    self.follow = False
                    self.scroll = 0
                elif token in (b"k", b"\x1b[A"):
                    self.selected = max(0, self.selected - 1)
                    self.follow = False
                    self.scroll = 0
                elif token == b"f":
                    self.follow = True
                    self.selected = max(0, len(self.actions) - 1)
            elif token in (b"j", b"\x1b[B"):
                self.scroll += 1
            elif token in (b"k", b"\x1b[A"):
                self.scroll = max(0, self.scroll - 1)

    def add(self, window, y, x, text, attribute=0):
        h, w = window.getmaxyx()
        if y >= h or x >= w or y < 0 or x < 0:
            return
        try:
            window.addstr(y, x, clip(text, max(0, w - x - 1)), attribute)
        except curses.error:
            pass

    def color(self, cell):
        attribute = ((curses.A_BOLD if cell.bold else 0) | (curses.A_REVERSE if cell.reverse else 0)
                     | (curses.A_UNDERLINE if cell.underline else 0)
                     | (getattr(curses, "A_ITALIC", 0) if cell.italic else 0))
        if not curses.has_colors():
            return attribute
        fg = cell.fg if cell.fg < curses.COLORS else cell.fg % 8
        bg = cell.bg if cell.bg < curses.COLORS else cell.bg % 8
        key = fg, bg
        if key not in self.pairs and len(self.pairs) + 1 < curses.COLOR_PAIRS:
            number = len(self.pairs) + 1
            try:
                curses.init_pair(number, fg, bg)
                self.pairs[key] = number
            except curses.error:
                pass
        return attribute | curses.color_pair(self.pairs.get(key, 0))

    def paint(self, screen, geometry):
        screen.erase()
        rows, columns = screen.getmaxyx()
        self.add(screen, 0, 0, "Labradour | %s | focus: %s | %s" % (
            Path(self.workspace).name, self.focus,
            "LIVE" if self.follow else "historical selection"), curses.A_BOLD)
        for name, rect in geometry.items():
            window = screen.derwin(rect.height, rect.width, rect.y, rect.x)
            if rect.height >= 2 and rect.width >= 2:
                window.box()
            self.add(window, 0, 2, " %s%s " % (name.title(), " [focus]" if self.focus == name else ""),
                     curses.A_BOLD)
            h, w = rect.content_size
            if name == "agent":
                for y, row in enumerate(self.terminal.grid[:h]):
                    for x, cell in enumerate(row[:w]):
                        if cell.text:
                            self.add(window, y + 1, x + 1, cell.text, self.color(cell))
            elif name == "activity":
                start = max(0, self.selected - h + 1)
                for index, action in enumerate(self.actions[start:start + h], start):
                    p = action["payload"]
                    state = {"PreToolUse": "running", "PostToolUse": "completed",
                             "PostToolUseFailure": "failed"}.get(p.get("hook_event_name"), p.get("hook_event_name", action.get("kind", "event")))
                    label = "%s %s %s" % (p.get("actor", "main/unknown"),
                                            p.get("tool_name", p.get("operation", "session")), state)
                    self.add(window, index - start + 1, 1, label,
                             curses.A_REVERSE if index == self.selected else 0)
                if not self.actions:
                    self.add(window, 1, 1, "No hook events; native tool coverage unverified")
            elif self.policy_view:
                lines = ["Effective capture policy", "Recording enabled" if self.recording else "Recording disabled"]
                lines += json.dumps(self.policy.describe(self.workspace, [p for p in (self.recording, self.events) if p]),
                                    ensure_ascii=False, indent=2).splitlines()
                for y, line in enumerate(lines[self.scroll:self.scroll + h]):
                    self.add(window, y + 1, 1, line)
            elif self.actions:
                p = self.actions[self.selected]["payload"]
                lines = ["View: " + p.get("operation", "tool/event card"),
                         "Evidence: " + ("watcher observation; no content snapshot" if
                                          p.get("quality") == "watcher-observed" else "durable checkpoint/event" if self.recorder else "hook payload (Phase 0)")]
                lines += json.dumps(p, ensure_ascii=False, indent=2).splitlines()
                for y, line in enumerate(lines[self.scroll:self.scroll + h]):
                    self.add(window, y + 1, 1, line)
            else:
                self.add(window, 1, 1, "Select an action; run the fake agent to emit events")
        footer = "Ctrl-Q quit | Ctrl-] ? help | " + self.notice
        if self.recorder:
            metrics = self.recorder.metrics
            footer += " | scan %.0fms | read %d/cache %d | queue %d" % (
                metrics.get("scan_ms", 0), metrics.get("read_files", 0), metrics.get("cache_hits", 0),
                metrics.get("watcher_queue", 0))
        if self.terminal.unsupported:
            footer += " | unsupported VT: %s" % len(self.terminal.unsupported)
        if self.router.prefix:
            footer = "Ctrl-Q quit | PREFIX: a/l/v focus | m mirror | z maximize | p policy | +/- width | [/] height | q quit"
        self.add(screen, rows - 1, 0, footer)
        if "agent" in geometry and self.focus == "agent" and self.terminal.cursor_visible:
            rect = geometry["agent"]
            y = rect.y + 1 + min(self.terminal.y, rect.content_size[0] - 1)
            x = rect.x + 1 + min(self.terminal.x, rect.content_size[1] - 1)
            try:
                curses.curs_set(1)
                screen.move(min(rows - 1, y), min(columns - 1, x))
            except curses.error:
                pass
        else:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        screen.refresh()

    def run(self, screen):
        curses.raw()
        curses.noecho()
        # Raw byte reads preserve native key/paste sequences.
        screen.keypad(False)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
        geometry = self.geometry(screen)
        h, w = geometry.get("agent", next(iter(geometry.values()), Rect(0, 0, 26, 82))).content_size
        self.terminal = Terminal(h, w)
        child_env = dict(os.environ, LABRADOUR_EVENTS_DIR=str(self.events.resolve()))
        self.child = PtyProcess(self.command, self.workspace, h, w, child_env, paused=bool(self.recording))
        # Fork the PTY child before starting watchdog's threads.
        old_handlers = {}
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, lambda *_: setattr(self, "running", False))
        os.write(1, b"\x1b[?2004h")
        try:
            if self.recording:
                from .recorder import Recorder
                self.notice = "Capture policy shown in Visualization | Ctrl-] p toggles policy"
                self.paint(screen, geometry)
                self.recorder = Recorder(self.workspace, self.recording, excluded=[self.events],
                                         backend=self.watch_backend, events=self.events, policy=self.policy)
                self.recorder.start()
                self.child.release()
            elif self.watch_enabled:
                from .filesystem import WorkspaceWatch
                self.watcher = WorkspaceWatch(self.workspace, excluded=[self.events], backend=self.watch_backend)
                self.watcher.start()
                self.notice = "pyte | watcher: %s | observations only" % self.watcher.backend
            while self.running:
                curses.update_lines_cols()
                actual = os.get_terminal_size(0)
                if screen.getmaxyx() != (actual.lines, actual.columns):
                    curses.resizeterm(actual.lines, actual.columns)
                geometry = self.geometry(screen)
                if "agent" in geometry:
                    h, w = geometry["agent"].content_size
                    self.child.resize(h, w)
                    if (self.terminal.rows, self.terminal.columns) != (h, w):
                        self.terminal.resize(h, w)
                # Hidden agent retains its previous usable PTY size.
                output = self.child.read()
                if output:
                    replies = self.terminal.feed(output)
                    if replies:
                        self.child.send(replies)
                self.child.flush()
                self.collect()
                if self.child.poll() is not None:
                    self.notice = ((self.recorder.notice + " | ") if self.recorder else "") + (
                        "Agent exited %s; review remains open. Ctrl-Q quits" % self.child.status)
                for kind, token in self.router.expire():
                    self.handle(kind, token)
                self.paint(screen, geometry)
                if select.select([0], [], [], .02)[0]:
                    data = os.read(0, 65536)
                    if not data:
                        break
                    for kind, token in self.router.feed(data):
                        self.handle(kind, token)
        finally:
            os.write(1, b"\x1b[?2004l")
            try:
                self.child.close()
                if self.recorder:
                    self.recorder.close()
                elif self.watcher:
                    self.watcher.close()
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
